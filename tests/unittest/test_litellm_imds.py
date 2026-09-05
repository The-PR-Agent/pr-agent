"""Tests for request-local AWS credentials used by LiteLLM Bedrock calls."""

import asyncio
import os
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import litellm
import openai
import pytest
from botocore.exceptions import ClientError, CredentialRetrievalError, ProfileNotFound

import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_handler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler


def _base_settings(overrides=None):
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
            "get": lambda self, key, default=None: default,
        })(),
        "get": lambda self, key, default=None: overrides.get(key, default),
    })()


def _static_aws_settings(prefix="STATIC", session_token=None, overrides=None):
    overrides = {
        "aws.AWS_ACCESS_KEY_ID": f"{prefix}-KEY",
        "aws.AWS_SECRET_ACCESS_KEY": f"{prefix}-SECRET",
        "aws.AWS_REGION_NAME": "us-east-1",
        **(overrides or {}),
    }
    if session_token:
        overrides["aws.AWS_SESSION_TOKEN"] = session_token
    return _base_settings(overrides)


def _frozen_creds(access_key="IMDS-KEY", secret_key="IMDS-SECRET", token=None):
    frozen = MagicMock()
    frozen.access_key = access_key
    frozen.secret_key = secret_key
    frozen.token = token
    return frozen


def _mock_response():
    mock = MagicMock()
    mock.__getitem__ = lambda self, key: {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]
    }[key]
    mock.dict.return_value = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
    return mock


@pytest.fixture(autouse=True)
def isolate_aws(monkeypatch):
    mixin = litellm_handler.BedrockMantleAuthMixin
    saved_mixin_signer = mixin.sign_request if mixin is not None else None
    saved_mixin_token_resolver = mixin._resolve_bearer_token if mixin is not None else None
    saved_original_signer = litellm_handler._bedrock_mantle_sign_request
    saved_original_token_resolver = litellm_handler._bedrock_mantle_resolve_bearer_token
    marker = litellm_handler._BEDROCK_MANTLE_ORIGINAL_SIGNER
    resolver_marker = litellm_handler._BEDROCK_MANTLE_ORIGINAL_TOKEN_RESOLVER
    missing = object()
    saved_marker = getattr(litellm_handler._sign_bedrock_mantle_request, marker, missing)
    saved_resolver_marker = getattr(
        litellm_handler._resolve_bedrock_mantle_bearer_token,
        resolver_marker,
        missing,
    )
    for variable in (
        *litellm_handler.AWS_CREDENTIAL_CHAIN_ENV_VARS,
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_REGION_NAME", "AWS_REGION",
        "AWS_DEFAULT_REGION", "AWS_USE_IMDS", "AWS_BEDROCK_RUNTIME_ENDPOINT", "BEDROCK_MANTLE_API_KEY",
        "AWS_BEARER_TOKEN_BEDROCK", "BEDROCK_MANTLE_API_BASE", "BEDROCK_MANTLE_REGION", "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _base_settings())
    yield
    if mixin is not None:
        mixin.sign_request = saved_mixin_signer
        mixin._resolve_bearer_token = staticmethod(saved_mixin_token_resolver)
    litellm_handler._bedrock_mantle_sign_request = saved_original_signer
    litellm_handler._bedrock_mantle_resolve_bearer_token = saved_original_token_resolver
    if saved_marker is missing:
        if hasattr(litellm_handler._sign_bedrock_mantle_request, marker):
            delattr(litellm_handler._sign_bedrock_mantle_request, marker)
    else:
        setattr(litellm_handler._sign_bedrock_mantle_request, marker, saved_marker)
    if saved_resolver_marker is missing:
        if hasattr(litellm_handler._resolve_bedrock_mantle_bearer_token, resolver_marker):
            delattr(litellm_handler._resolve_bedrock_mantle_bearer_token, resolver_marker)
    else:
        setattr(
            litellm_handler._resolve_bedrock_mantle_bearer_token,
            resolver_marker,
            saved_resolver_marker,
        )


async def _call(handler, model="bedrock/anthropic.claude-sonnet-4-5"):
    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        await handler.chat_completion(model=model, system="sys", user="usr")
    return mock_call.call_args.kwargs


@pytest.mark.asyncio
async def test_bedrock_runtime_endpoint_is_request_local(monkeypatch):
    endpoint = "https://vpce.example.com"
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _static_aws_settings(overrides={"aws.AWS_BEDROCK_RUNTIME_ENDPOINT": endpoint}),
    )

    kwargs = await _call(LiteLLMAIHandler())

    assert kwargs["aws_bedrock_runtime_endpoint"] == endpoint
    assert "AWS_BEDROCK_RUNTIME_ENDPOINT" not in os.environ


@pytest.mark.asyncio
async def test_bedrock_runtime_endpoint_setting_overrides_ambient_env(monkeypatch):
    monkeypatch.setenv("AWS_BEDROCK_RUNTIME_ENDPOINT", "https://environment.example.com")
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _static_aws_settings(
            overrides={"aws.AWS_BEDROCK_RUNTIME_ENDPOINT": "https://settings.example.com"}
        ),
    )

    kwargs = await _call(LiteLLMAIHandler())

    assert kwargs["aws_bedrock_runtime_endpoint"] == "https://settings.example.com"
    assert os.environ["AWS_BEDROCK_RUNTIME_ENDPOINT"] == "https://environment.example.com"


@pytest.mark.asyncio
async def test_bedrock_runtime_endpoint_environment_is_request_local(monkeypatch):
    monkeypatch.setenv("AWS_BEDROCK_RUNTIME_ENDPOINT", "https://tenant-a.example.com")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "request-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "request-secret")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("AWS_BEDROCK_RUNTIME_ENDPOINT", "https://tenant-b.example.com")

    kwargs = await _call(handler)

    assert kwargs["aws_bedrock_runtime_endpoint"] == "https://tenant-a.example.com"
    assert os.environ["AWS_BEDROCK_RUNTIME_ENDPOINT"] == "https://tenant-b.example.com"


@pytest.mark.asyncio
@pytest.mark.skipif(
    litellm_handler.BedrockMantleAuthMixin is None,
    reason="Installed LiteLLM does not provide BedrockMantleAuthMixin",
)
async def test_bedrock_mantle_runtime_endpoint_is_request_local(monkeypatch):
    endpoint = "https://vpce.example.com"
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _static_aws_settings(overrides={"aws.AWS_BEDROCK_RUNTIME_ENDPOINT": endpoint}),
    )

    kwargs = await _call(LiteLLMAIHandler(), model="bedrock_mantle/openai.gpt-oss-120b")

    assert kwargs["aws_bedrock_runtime_endpoint"] == endpoint
    assert "AWS_BEDROCK_RUNTIME_ENDPOINT" not in os.environ


@pytest.mark.asyncio
async def test_static_credentials_are_forwarded_without_mutating_env(monkeypatch):
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _static_aws_settings(session_token="STATIC-TOKEN"))
    monkeypatch.setenv("AWS_SESSION_TOKEN", "ambient-token")
    handler = LiteLLMAIHandler()

    kwargs = await _call(handler)

    assert kwargs["aws_access_key_id"] == "STATIC-KEY"
    assert kwargs["aws_secret_access_key"] == "STATIC-SECRET"
    assert kwargs["aws_session_token"] == "STATIC-TOKEN"
    assert kwargs["aws_region_name"] == "us-east-1"
    assert os.environ["AWS_SESSION_TOKEN"] == "ambient-token"
    assert "AWS_ACCESS_KEY_ID" not in os.environ


@pytest.mark.parametrize("model", (
    "sagemaker/my-endpoint",
    pytest.param(
        "bedrock_mantle/openai.gpt-oss-120b",
        marks=pytest.mark.skipif(
            litellm_handler.BedrockMantleAuthMixin is None,
            reason="Installed LiteLLM does not provide BedrockMantleAuthMixin",
        ),
    ),
    pytest.param(
        "bedrock_mantle/xai.grok-4.3",
        marks=pytest.mark.skipif(
            litellm_handler.BedrockMantleAuthMixin is None,
            reason="Installed LiteLLM does not provide BedrockMantleAuthMixin",
        ),
    ),
))
@pytest.mark.asyncio
async def test_static_credentials_are_forwarded_to_additional_aws_providers(monkeypatch, model):
    monkeypatch.setattr(litellm_handler, "get_settings", _static_aws_settings)
    monkeypatch.setenv("AWS_SESSION_TOKEN", "ambient-token")

    kwargs = await _call(LiteLLMAIHandler(), model=model)

    assert kwargs["aws_access_key_id"] == "STATIC-KEY"
    assert kwargs["aws_secret_access_key"] == "STATIC-SECRET"
    assert kwargs["aws_session_token"] == ""
    assert kwargs["aws_region_name"] == "us-east-1"
    assert os.environ["AWS_SESSION_TOKEN"] == "ambient-token"


@pytest.mark.asyncio
@pytest.mark.skipif(
    litellm_handler.BedrockMantleAuthMixin is None,
    reason="Installed LiteLLM does not provide BedrockMantleAuthMixin",
)
async def test_bedrock_mantle_signer_receives_request_local_credentials(monkeypatch):
    from litellm.llms.bedrock_mantle.chat.transformation import BedrockMantleChatConfig

    monkeypatch.setattr(litellm_handler, "get_settings", _static_aws_settings)
    monkeypatch.setenv("BEDROCK_MANTLE_API_BASE", "https://bedrock-mantle.eu-west-1.api.aws")
    handler = LiteLLMAIHandler()
    aws_signer = MagicMock()
    aws_signer._sign_request.return_value = ({}, None)
    config = BedrockMantleChatConfig(aws_signer=aws_signer)

    async def capture_call(**kwargs):
        config.sign_request(
            headers={},
            optional_params={"aws_access_key_id": None, "aws_region_name": None, "unrelated": "value"},
            request_data={},
            api_base=f"{kwargs['api_base']}/v1/chat/completions",
        )
        return _mock_response()

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", side_effect=capture_call):
        await handler.chat_completion(
            model="bedrock_mantle/openai.gpt-oss-120b",
            system="sys",
            user="usr",
        )

    assert aws_signer._sign_request.call_args.kwargs["optional_params"] == {
        "aws_access_key_id": "STATIC-KEY",
        "aws_secret_access_key": "STATIC-SECRET",
        "aws_session_token": "",
        "aws_region_name": "eu-west-1",
        "unrelated": "value",
    }
    assert litellm_handler._bedrock_mantle_request_credentials.get() is None


@pytest.mark.asyncio
@pytest.mark.skipif(
    litellm_handler.BedrockMantleAuthMixin is None,
    reason="Installed LiteLLM does not provide BedrockMantleAuthMixin",
)
async def test_concurrent_bedrock_mantle_signers_keep_credentials_isolated(monkeypatch):
    from litellm.llms.bedrock_mantle.chat.transformation import BedrockMantleChatConfig

    monkeypatch.setattr(litellm, "api_key", "another-request-key")
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _static_aws_settings("TENANT-A"))
    tenant_a = LiteLLMAIHandler()
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _static_aws_settings("TENANT-B"))
    tenant_b = LiteLLMAIHandler()
    aws_signer = MagicMock()
    aws_signer._sign_request.return_value = ({}, None)
    config = BedrockMantleChatConfig(aws_signer=aws_signer)
    both_started = asyncio.Event()
    started = 0

    async def capture_call(**kwargs):
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=5)
        config.sign_request(
            headers={},
            optional_params={},
            request_data={},
            api_base="https://bedrock-mantle.us-east-1.api.aws/v1/chat/completions",
            api_key=kwargs["api_key"],
        )
        return _mock_response()

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", side_effect=capture_call):
        await asyncio.gather(
            tenant_a.chat_completion(
                model="bedrock_mantle/openai.gpt-oss-120b",
                system="sys",
                user="usr",
            ),
            tenant_b.chat_completion(
                model="bedrock_mantle/openai.gpt-oss-120b",
                system="sys",
                user="usr",
            ),
        )

    signer_credentials = {
        (call.kwargs["optional_params"]["aws_access_key_id"], call.kwargs["api_key"])
        for call in aws_signer._sign_request.call_args_list
    }
    assert signer_credentials == {("TENANT-A-KEY", ""), ("TENANT-B-KEY", "")}
    assert litellm_handler._bedrock_mantle_request_credentials.get() is None
    assert litellm_handler._bedrock_mantle_block_bearer.get() is False


@pytest.mark.asyncio
@pytest.mark.skipif(
    litellm_handler.BedrockMantleAuthMixin is None,
    reason="Installed LiteLLM does not provide BedrockMantleAuthMixin",
)
async def test_custom_bedrock_mantle_provider_uses_request_local_signer(monkeypatch):
    from litellm.llms.bedrock_mantle.chat.transformation import BedrockMantleChatConfig

    settings = _static_aws_settings()
    settings.litellm.custom_llm_provider = "bedrock_mantle"
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    aws_signer = MagicMock()
    aws_signer._sign_request.return_value = ({}, None)
    config = BedrockMantleChatConfig(aws_signer=aws_signer)

    async def capture_call(**kwargs):
        config.sign_request(
            headers={},
            optional_params={},
            request_data={},
            api_base="https://bedrock-mantle.us-east-1.api.aws/v1/chat/completions",
        )
        return _mock_response()

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", side_effect=capture_call):
        await LiteLLMAIHandler().chat_completion(model="hosted-model", system="sys", user="usr")

    assert aws_signer._sign_request.call_args.kwargs["optional_params"]["aws_access_key_id"] == "STATIC-KEY"


@pytest.mark.asyncio
@pytest.mark.skipif(
    litellm_handler.BedrockMantleAuthMixin is None,
    reason="Installed LiteLLM does not provide BedrockMantleAuthMixin",
)
async def test_bedrock_mantle_blocks_residual_key_without_disabling_sigv4(monkeypatch):
    from litellm.llms.bedrock_mantle.chat.transformation import BedrockMantleChatConfig

    monkeypatch.setattr(litellm_handler, "get_settings", _static_aws_settings)
    monkeypatch.setattr(litellm, "api_key", "another-request-key")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "late-request-token")
    aws_signer = MagicMock()
    aws_signer._sign_request.return_value = ({}, None)
    config = BedrockMantleChatConfig(aws_signer=aws_signer)

    async def capture_call(**kwargs):
        assert kwargs["api_key"] == litellm_handler.DUMMY_LITELLM_API_KEY
        config.sign_request(
            headers={},
            optional_params={},
            request_data={},
            api_base="https://bedrock-mantle.us-east-1.api.aws/v1/chat/completions",
            api_key=kwargs["api_key"],
        )
        return _mock_response()

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", side_effect=capture_call):
        await handler.chat_completion(
            model="bedrock_mantle/openai.gpt-oss-120b",
            system="sys",
            user="usr",
        )

    assert aws_signer._sign_request.call_args.kwargs["api_key"] == ""
    assert litellm_handler._bedrock_mantle_request_credentials.get() is None
    assert litellm_handler._bedrock_mantle_block_bearer.get() is False


@pytest.mark.asyncio
@pytest.mark.skipif(
    litellm_handler.BedrockMantleAuthMixin is None,
    reason="Installed LiteLLM does not provide BedrockMantleAuthMixin",
)
async def test_bedrock_mantle_real_dispatch_blocks_residual_bearer(monkeypatch):
    from litellm.llms.bedrock.base_aws_llm import BaseAWSLLM

    monkeypatch.setattr(litellm, "api_key", "another-request-key")
    handler = LiteLLMAIHandler.__new__(LiteLLMAIHandler)
    handler._request_provider_cache = {}
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "late-request-token")
    signer_calls = []

    def stop_before_network(self, *args, **kwargs):
        signer_calls.append(kwargs)
        raise RuntimeError("stop before network")

    monkeypatch.setattr(BaseAWSLLM, "_sign_request", stop_before_network)

    with pytest.raises(openai.APIConnectionError):
        await handler._acompletion(
            model="bedrock_mantle/openai.gpt-oss-120b",
            api_key=litellm_handler.DUMMY_LITELLM_API_KEY,
            aws_access_key_id="request-key",
            aws_secret_access_key="request-secret",
            aws_session_token="request-token",
            aws_region_name="us-east-1",
            aws_bedrock_runtime_endpoint="https://vpce.example.com",
            messages=[{"role": "user", "content": "ping"}],
        )

    assert len(signer_calls) == 1
    assert signer_calls[0]["api_key"] == ""
    assert signer_calls[0]["optional_params"]["aws_access_key_id"] == "request-key"
    assert signer_calls[0]["optional_params"]["aws_bedrock_runtime_endpoint"] == "https://vpce.example.com"
    assert not set(litellm_handler.BEDROCK_MANTLE_REQUEST_BODY_EXCLUDED_KEYS).intersection(
        signer_calls[0]["request_data"]
    )
    assert litellm_handler._bedrock_mantle_request_credentials.get() is None
    assert litellm_handler._bedrock_mantle_block_bearer.get() is False


@pytest.mark.asyncio
async def test_bedrock_sigv4_rejects_residual_litellm_api_key(monkeypatch):
    monkeypatch.setattr(litellm_handler, "get_settings", _static_aws_settings)
    monkeypatch.setattr(litellm, "api_key", "another-request-key")

    with pytest.raises(ValueError, match="Refusing process-wide Bedrock bearer token fallback"):
        await _call(LiteLLMAIHandler())


def test_bedrock_mantle_signer_bridge_forwards_future_arguments(monkeypatch):
    forwarded = {}

    def sign_request(self, headers, optional_params, request_data, api_base, **kwargs):
        forwarded["self"] = self
        forwarded["args"] = (headers, optional_params, request_data, api_base)
        forwarded["kwargs"] = kwargs
        return "signed"

    monkeypatch.setattr(litellm_handler, "_bedrock_mantle_sign_request", sign_request)
    request_credentials = {
        "aws_access_key_id": "request-key",
        "aws_secret_access_key": "request-secret",
    }
    token = litellm_handler._bedrock_mantle_request_credentials.set(request_credentials)
    signer = object()
    try:
        result = litellm_handler._sign_bedrock_mantle_request(
            signer,
            {},
            {"unrelated": "value"},
            {},
            "https://bedrock-mantle.us-east-1.api.aws/v1/chat/completions",
            future_argument="future-value",
        )
    finally:
        litellm_handler._bedrock_mantle_request_credentials.reset(token)

    assert result == "signed"
    assert forwarded["self"] is signer
    assert forwarded["args"][1] == {"unrelated": "value", **request_credentials}
    assert forwarded["kwargs"] == {"future_argument": "future-value"}


def test_bedrock_mantle_signer_bridge_fails_closed_without_optional_params(monkeypatch):
    sign_request = MagicMock()
    monkeypatch.setattr(litellm_handler, "_bedrock_mantle_sign_request", sign_request)
    token = litellm_handler._bedrock_mantle_request_credentials.set({"aws_access_key_id": "request-key"})
    try:
        with pytest.raises(RuntimeError, match="did not receive optional_params"):
            litellm_handler._sign_bedrock_mantle_request(object(), headers={}, request_data={})
    finally:
        litellm_handler._bedrock_mantle_request_credentials.reset(token)

    sign_request.assert_not_called()


@pytest.mark.asyncio
async def test_health_probe_refreshes_imds_credentials_once(monkeypatch):
    handler = LiteLLMAIHandler()
    handler._aws_use_imds = True
    handler._aws_imds_initialized = True
    handler._aws_imds_mode = True
    handler._aws_active_creds = {
        "aws_access_key_id": "stale-key",
        "aws_secret_access_key": "stale-secret",
        "aws_region_name": "us-east-1",
    }

    def refresh_credentials():
        handler._aws_active_creds = {
            "aws_access_key_id": "refreshed-key",
            "aws_secret_access_key": "refreshed-secret",
            "aws_region_name": "us-east-1",
        }
        return True

    monkeypatch.setattr(handler, "_refresh_aws_imds_credentials", refresh_credentials)
    completion = AsyncMock(return_value=_mock_response())

    await handler.probe_completion("bedrock/anthropic.claude-3-sonnet", _completion=completion)

    completion.assert_awaited_once()
    assert completion.call_args.kwargs["aws_access_key_id"] == "refreshed-key"
    assert completion.call_args.kwargs["aws_secret_access_key"] == "refreshed-secret"


@pytest.mark.skipif(
    litellm_handler.BedrockMantleAuthMixin is None,
    reason="Installed LiteLLM does not provide BedrockMantleAuthMixin",
)
def test_bedrock_mantle_signer_bridge_installation_is_idempotent():
    original_signer = litellm_handler._bedrock_mantle_sign_request

    litellm_handler._install_bedrock_mantle_signer_bridge()
    litellm_handler._install_bedrock_mantle_signer_bridge()

    assert litellm_handler._bedrock_mantle_sign_request is original_signer


@pytest.mark.asyncio
async def test_bedrock_mantle_fails_safely_when_signer_bridge_is_unavailable(monkeypatch):
    handler = LiteLLMAIHandler()
    completion = AsyncMock()
    monkeypatch.setattr(litellm_handler, "BedrockMantleAuthMixin", None)

    with pytest.raises(RuntimeError, match="does not provide the Bedrock Mantle signer bridge"):
        await handler._acompletion(
            _completion=completion,
            model="bedrock_mantle/openai.gpt-oss-120b",
            aws_access_key_id="request-key",
            aws_secret_access_key="request-secret",
            aws_session_token="",
            aws_region_name="us-east-1",
        )

    completion.assert_not_awaited()


@pytest.mark.asyncio
async def test_bedrock_mantle_uses_ambient_credentials_without_signer_bridge(monkeypatch):
    handler = LiteLLMAIHandler()
    completion = AsyncMock(return_value="response")
    monkeypatch.setattr(litellm_handler, "BedrockMantleAuthMixin", None)

    response = await handler._acompletion(
        _completion=completion,
        model="bedrock_mantle/openai.gpt-oss-120b",
    )

    assert response == "response"
    completion.assert_awaited_once_with(model="bedrock_mantle/openai.gpt-oss-120b")


@pytest.mark.asyncio
@pytest.mark.parametrize("session_token", (None, "tenant-a-token"))
@pytest.mark.parametrize("region_variable", ("AWS_REGION_NAME", "AWS_REGION"))
async def test_native_aws_environment_is_snapshotted_per_handler(monkeypatch, session_token, region_variable):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "tenant-a-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "tenant-a-secret")
    monkeypatch.setenv(region_variable, "ap-northeast-1")
    if session_token:
        monkeypatch.setenv("AWS_SESSION_TOKEN", session_token)
    tenant_a = LiteLLMAIHandler()
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "tenant-b-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "tenant-b-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "tenant-b-token")
    monkeypatch.setenv(region_variable, "us-west-2")
    tenant_b = LiteLLMAIHandler()

    tenant_a_kwargs = await _call(tenant_a)
    tenant_b_kwargs = await _call(tenant_b)

    assert tenant_a_kwargs["aws_access_key_id"] == "tenant-a-key"
    assert tenant_a_kwargs["aws_secret_access_key"] == "tenant-a-secret"
    assert tenant_a_kwargs["aws_session_token"] == (session_token or "")
    assert tenant_a_kwargs["aws_region_name"] == "ap-northeast-1"
    assert tenant_b_kwargs["aws_access_key_id"] == "tenant-b-key"
    assert tenant_b_kwargs["aws_secret_access_key"] == "tenant-b-secret"
    assert tenant_b_kwargs["aws_session_token"] == "tenant-b-token"
    assert tenant_b_kwargs["aws_region_name"] == "us-west-2"
    assert os.environ["AWS_ACCESS_KEY_ID"] == "tenant-b-key"
    assert os.environ["AWS_SECRET_ACCESS_KEY"] == "tenant-b-secret"
    assert os.environ["AWS_SESSION_TOKEN"] == "tenant-b-token"
    assert os.environ[region_variable] == "us-west-2"


@pytest.mark.asyncio
async def test_native_aws_security_token_precedes_session_token(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "request-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "request-secret")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "request-security-token")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "request-session-token")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    kwargs = await _call(LiteLLMAIHandler())

    assert kwargs["aws_session_token"] == "request-security-token"


@pytest.mark.parametrize("model", ("sagemaker_chat/model", "sagemaker_nova/model"))
@pytest.mark.asyncio
async def test_sagemaker_chat_rejects_bedrock_bearer_before_litellm(monkeypatch, model):
    from litellm.llms.sagemaker.chat.transformation import SagemakerChatConfig

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "request-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "request-secret")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-only-token")
    config = SagemakerChatConfig()
    headers, _ = config.sign_request(
        headers={},
        optional_params={
            "aws_access_key_id": "request-key",
            "aws_secret_access_key": "request-secret",
            "aws_region_name": "us-east-1",
        },
        request_data={},
        api_base="https://runtime.sagemaker.us-east-1.amazonaws.com/endpoints/model/invocations",
        api_key=litellm_handler.DUMMY_LITELLM_API_KEY,
    )
    assert headers["Authorization"] == "Bearer bedrock-only-token"

    completion = AsyncMock(return_value=_mock_response())
    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new=completion):
        with pytest.raises(ValueError, match="Bedrock bearer token fallback for SageMaker"):
            await LiteLLMAIHandler().chat_completion(
                model=model,
                system="sys",
                user="usr",
            )

    completion.assert_not_awaited()


@pytest.mark.asyncio
async def test_aws_request_without_credential_snapshot_fails_closed(monkeypatch):
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "another-request-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "another-request-secret")

    with pytest.raises(ValueError, match="AWS credentials were not resolved for this request"):
        await _call(handler)


@pytest.mark.asyncio
async def test_native_aws_environment_without_region_fails_closed(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "request-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "request-secret")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("AWS_REGION", "another-request-region")

    await _call(handler, model="gpt-4o")
    with pytest.raises(ValueError, match="AWS region was not resolved for this request"):
        await _call(handler)


@pytest.mark.parametrize(
    ("environment_variable", "settings_region"),
    (
        ("AWS_DEFAULT_REGION", None),
        (None, "eu-west-1"),
    ),
)
@pytest.mark.asyncio
async def test_native_aws_environment_snapshots_supported_region_sources(
    monkeypatch,
    environment_variable,
    settings_region,
):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "request-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "request-secret")
    if environment_variable:
        monkeypatch.setenv(environment_variable, "eu-west-1")
    if settings_region:
        monkeypatch.setattr(
            litellm_handler,
            "get_settings",
            lambda: _base_settings({"aws.AWS_REGION_NAME": settings_region}),
        )

    kwargs = await _call(LiteLLMAIHandler())

    assert kwargs["aws_region_name"] == "eu-west-1"


@pytest.mark.parametrize(
    ("settings_region", "expected_region"),
    (
        ("eu-central-1", "eu-central-1"),
        (None, "ap-northeast-1"),
    ),
)
@pytest.mark.asyncio
async def test_native_aws_environment_region_source_precedence(monkeypatch, settings_region, expected_region):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "request-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "request-secret")
    monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    if settings_region:
        monkeypatch.setattr(
            litellm_handler,
            "get_settings",
            lambda: _base_settings({"aws.AWS_REGION_NAME": settings_region}),
        )

    kwargs = await _call(LiteLLMAIHandler())

    assert kwargs["aws_region_name"] == expected_region


@pytest.mark.parametrize("missing_variable", ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"))
@pytest.mark.asyncio
async def test_incomplete_native_aws_environment_credentials_only_raise_for_aws(monkeypatch, missing_variable):
    values = {
        "AWS_ACCESS_KEY_ID": "ambient-key",
        "AWS_SECRET_ACCESS_KEY": "ambient-secret",
    }
    values.pop(missing_variable)
    for variable, value in values.items():
        monkeypatch.setenv(variable, value)

    handler = LiteLLMAIHandler()

    await _call(handler, model="gpt-4o")
    with pytest.raises(ValueError, match="AWS environment credentials are incomplete"):
        await _call(handler)


@pytest.mark.parametrize("use_imds", (False, True))
@pytest.mark.asyncio
async def test_incomplete_aws_environment_does_not_block_static_credentials(monkeypatch, use_imds):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "incomplete-environment-key")
    if use_imds:
        monkeypatch.setenv("AWS_USE_IMDS", "true")
    monkeypatch.setattr(litellm_handler, "get_settings", _static_aws_settings)

    with patch("boto3.Session") as session_factory:
        kwargs = await _call(LiteLLMAIHandler())

    session_factory.assert_not_called()
    assert kwargs["aws_access_key_id"] == "STATIC-KEY"
    assert kwargs["aws_secret_access_key"] == "STATIC-SECRET"
    assert kwargs["aws_session_token"] == ""
    assert kwargs["aws_region_name"] == "us-east-1"


@pytest.mark.parametrize("missing_variable", ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"))
@pytest.mark.asyncio
async def test_bedrock_mantle_bearer_ignores_incomplete_sigv4_environment(monkeypatch, missing_variable):
    values = {
        "AWS_ACCESS_KEY_ID": "ambient-key",
        "AWS_SECRET_ACCESS_KEY": "ambient-secret",
    }
    values.pop(missing_variable)
    for variable, value in values.items():
        monkeypatch.setenv(variable, value)
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "request-bearer-token")

    kwargs = await _call(LiteLLMAIHandler(), model="bedrock_mantle/openai.gpt-oss-120b")

    assert kwargs["api_key"] == "request-bearer-token"
    assert "aws_access_key_id" not in kwargs
    assert "aws_secret_access_key" not in kwargs


@pytest.mark.asyncio
async def test_bedrock_mantle_bearer_ignores_complete_sigv4_environment_without_region(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "request-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "request-secret")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "request-bearer-token")

    kwargs = await _call(LiteLLMAIHandler(), model="bedrock_mantle/openai.gpt-oss-120b")

    assert kwargs["api_key"] == "request-bearer-token"
    assert "aws_access_key_id" not in kwargs
    assert "aws_secret_access_key" not in kwargs
    assert "aws_region_name" not in kwargs


@pytest.mark.parametrize("probe", (False, True))
@pytest.mark.asyncio
async def test_bedrock_mantle_bearer_skips_imds_discovery(monkeypatch, probe):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "request-bearer-token")
    handler = LiteLLMAIHandler()
    initialize = MagicMock(side_effect=AssertionError("IMDS discovery should not run"))
    monkeypatch.setattr(handler, "_initialize_aws_imds_credentials", initialize)

    if probe:
        completion = AsyncMock(return_value=_mock_response())
        await handler.probe_completion(
            "bedrock_mantle/openai.gpt-oss-120b",
            _completion=completion,
        )
        kwargs = completion.call_args.kwargs
    else:
        kwargs = await _call(handler, model="bedrock_mantle/openai.gpt-oss-120b")

    initialize.assert_not_called()
    assert kwargs["api_key"] == "request-bearer-token"
    assert "aws_access_key_id" not in kwargs
    assert "aws_secret_access_key" not in kwargs


@pytest.mark.parametrize("probe", (False, True))
@pytest.mark.asyncio
async def test_bedrock_bearer_skips_imds_discovery(monkeypatch, probe):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "request-bearer-token")
    monkeypatch.setenv("AWS_REGION_NAME", "us-east-1")
    handler = LiteLLMAIHandler()
    initialize = MagicMock(side_effect=AssertionError("IMDS discovery should not run"))
    monkeypatch.setattr(handler, "_initialize_aws_imds_credentials", initialize)

    if probe:
        completion = AsyncMock(return_value=_mock_response())
        await handler.probe_completion(
            "bedrock/model",
            _completion=completion,
        )
        kwargs = completion.call_args.kwargs
    else:
        kwargs = await _call(handler, model="bedrock/model")

    initialize.assert_not_called()
    assert kwargs["api_key"] == "request-bearer-token"
    assert kwargs["aws_region_name"] == "us-east-1"
    assert "aws_access_key_id" not in kwargs
    assert "aws_secret_access_key" not in kwargs


def test_incomplete_static_credentials_raise(monkeypatch):
    settings = _base_settings({"aws.AWS_ACCESS_KEY_ID": "incomplete"})
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)

    with pytest.raises(ValueError, match="AWS credentials are incomplete"):
        LiteLLMAIHandler()


def test_incomplete_static_credentials_warn_when_imds_is_enabled(monkeypatch):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    settings = _base_settings({"aws.AWS_ACCESS_KEY_ID": "incomplete"})
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: settings)
    logger = MagicMock()
    monkeypatch.setattr(litellm_handler, "get_logger", lambda: logger)

    LiteLLMAIHandler()

    logger.warning.assert_called_once_with(
        "AWS_USE_IMDS is set but configured static AWS credentials are incomplete; "
        "no static fallback is available"
    )


@pytest.mark.asyncio
async def test_imds_credentials_are_captured_without_blocking_initialization(monkeypatch):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "ambient-token")
    frozen = _frozen_creds(token="imds-token")
    session = MagicMock()
    session.get_credentials.return_value.get_frozen_credentials.return_value = frozen
    session.region_name = "eu-west-1"

    with patch("boto3.Session", return_value=session):
        handler = LiteLLMAIHandler()
        session.get_credentials.assert_not_called()
        await _call(handler)

    assert handler._aws_active_creds == {
        "aws_access_key_id": "IMDS-KEY",
        "aws_secret_access_key": "IMDS-SECRET",
        "aws_session_token": "imds-token",
        "aws_region_name": "eu-west-1",
    }
    assert handler._aws_imds_mode is True
    assert os.environ["AWS_SESSION_TOKEN"] == "ambient-token"
    assert "AWS_ACCESS_KEY_ID" not in os.environ


@pytest.mark.asyncio
async def test_imds_mode_snapshots_explicit_environment_credentials(monkeypatch):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "tenant-a-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "tenant-a-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "tenant-a-token")
    monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "tenant-b-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "tenant-b-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "tenant-b-token")

    def create_session(**kwargs):
        session = MagicMock()
        session.get_credentials.return_value.get_frozen_credentials.return_value = _frozen_creds(
            access_key=kwargs["aws_access_key_id"],
            secret_key=kwargs["aws_secret_access_key"],
            token=kwargs.get("aws_session_token"),
        )
        session.region_name = kwargs.get("region_name")
        return session

    with patch("boto3.Session", side_effect=create_session) as session_factory:
        kwargs = await _call(handler)

    session_factory.assert_called_once_with(
        aws_access_key_id="tenant-a-key",
        aws_secret_access_key="tenant-a-secret",
        aws_session_token="tenant-a-token",
        region_name="ap-northeast-1",
    )
    assert kwargs["aws_access_key_id"] == "tenant-a-key"
    assert kwargs["aws_secret_access_key"] == "tenant-a-secret"
    assert kwargs["aws_session_token"] == "tenant-a-token"
    assert kwargs["aws_region_name"] == "ap-northeast-1"


@pytest.mark.asyncio
async def test_imds_mode_rejects_late_environment_credentials(monkeypatch):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "another-request-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "another-request-secret")

    with patch("boto3.Session") as session_factory:
        with pytest.raises(ValueError, match="Refusing live AWS credential environment fallback"):
            await _call(handler)

    session_factory.assert_not_called()


@pytest.mark.parametrize("environment_variable", litellm_handler.AWS_CREDENTIAL_CHAIN_ENV_VARS)
@pytest.mark.parametrize("use_environment_credentials", (False, True))
@pytest.mark.asyncio
async def test_imds_mode_rejects_late_credential_chain_environment(
    monkeypatch,
    environment_variable,
    use_environment_credentials,
):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    if use_environment_credentials:
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "tenant-a-key")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "tenant-a-secret")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv(environment_variable, "another-request-value")

    with patch("boto3.Session") as session_factory:
        with pytest.raises(ValueError, match="Refusing changed AWS credential-chain environment"):
            await _call(handler)

    session_factory.assert_not_called()


@pytest.mark.parametrize(
    "environment_variable",
    ("AWS_ENDPOINT_URL_BEDROCK_RUNTIME", "AWS_ENDPOINT_URL_SAGEMAKER_RUNTIME"),
)
@pytest.mark.asyncio
async def test_imds_mode_rejects_late_service_endpoint_environment(monkeypatch, environment_variable):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv(environment_variable, "https://another-request.example.com")

    with patch("boto3.Session") as session_factory:
        with pytest.raises(ValueError, match="Refusing changed AWS credential-chain environment"):
            await _call(handler)

    session_factory.assert_not_called()


@pytest.mark.parametrize(
    ("model", "environment_variable"),
    (
        ("bedrock/anthropic.claude-sonnet-4-5", "AWS_ENDPOINT_URL"),
        ("bedrock/anthropic.claude-sonnet-4-5", "AWS_ENDPOINT_URL_BEDROCK_RUNTIME"),
        ("sagemaker/my-endpoint", "AWS_ENDPOINT_URL_SAGEMAKER_RUNTIME"),
    ),
)
@pytest.mark.asyncio
async def test_static_mode_rejects_late_aws_request_endpoint_environment(
    monkeypatch,
    model,
    environment_variable,
):
    monkeypatch.setattr(litellm_handler, "get_settings", _static_aws_settings)
    handler = LiteLLMAIHandler()
    monkeypatch.setenv(environment_variable, "https://another-request.example.com")

    with pytest.raises(ValueError, match="Refusing changed AWS request endpoint environment"):
        await _call(handler, model=model)


@pytest.mark.parametrize("environment_variable", ("AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE"))
@pytest.mark.asyncio
async def test_imds_mode_rejects_changed_credential_chain_file(monkeypatch, tmp_path, environment_variable):
    credential_chain_file = tmp_path / environment_variable.lower()
    credential_chain_file.write_text("request-a", encoding="utf-8")
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    monkeypatch.setenv(environment_variable, str(credential_chain_file))
    handler = LiteLLMAIHandler()
    credential_chain_file.write_text("request-b", encoding="utf-8")

    with patch("boto3.Session") as session_factory:
        with pytest.raises(ValueError, match="Refusing changed AWS credential-chain file"):
            await _call(handler)

    session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_imds_mode_rejects_late_original_ec2_credential_file(monkeypatch, tmp_path):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    handler = LiteLLMAIHandler()
    monkeypatch.setenv("AWS_CREDENTIAL_FILE", str(tmp_path / "credentials"))

    with patch("boto3.Session") as session_factory:
        with pytest.raises(ValueError, match="Refusing changed AWS credential-chain environment"):
            await _call(handler)

    session_factory.assert_not_called()


def test_original_ec2_credential_file_content_is_isolated(monkeypatch, tmp_path):
    credentials_file = tmp_path / "credentials"
    credentials_file.write_text("AWSAccessKeyId=request-a", encoding="utf-8")
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    monkeypatch.setenv("AWS_CREDENTIAL_FILE", str(credentials_file))
    handler = LiteLLMAIHandler()
    credentials_file.write_text("AWSAccessKeyId=request-b", encoding="utf-8")

    with pytest.raises(ValueError, match="Refusing changed AWS credential-chain file"):
        handler._validate_aws_credential_chain_environment()


def test_original_ec2_credential_file_rejects_non_files(monkeypatch, tmp_path):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    monkeypatch.setenv("AWS_CREDENTIAL_FILE", str(tmp_path))

    with pytest.raises(ValueError, match="AWS_CREDENTIAL_FILE must reference a regular file"):
        LiteLLMAIHandler()


def test_credential_chain_file_paths_include_boto_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AWS_SHARED_CREDENTIALS_FILE", raising=False)
    monkeypatch.delenv("AWS_CONFIG_FILE", raising=False)
    monkeypatch.delenv("BOTO_CONFIG", raising=False)

    assert LiteLLMAIHandler._aws_credential_chain_file_paths() == (
        str(tmp_path / ".aws" / "credentials"),
        str(tmp_path / ".aws" / "config"),
        "/etc/boto.cfg",
        str(tmp_path / ".boto"),
    )


@pytest.mark.parametrize(
    "environment_variable",
    ("AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE", "BOTO_CONFIG"),
)
def test_credential_chain_file_paths_preserve_empty_selectors(monkeypatch, tmp_path, environment_variable):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(environment_variable, "")

    paths = LiteLLMAIHandler._aws_credential_chain_file_paths()

    if environment_variable == "AWS_SHARED_CREDENTIALS_FILE":
        assert str(tmp_path / ".aws" / "credentials") not in paths
    elif environment_variable == "AWS_CONFIG_FILE":
        assert str(tmp_path / ".aws" / "config") not in paths
    else:
        assert "/etc/boto.cfg" not in paths
        assert str(tmp_path / ".boto") not in paths


@pytest.mark.parametrize(
    "environment_variable",
    ("AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE", "BOTO_CONFIG"),
)
def test_credential_chain_file_paths_expand_environment_variables(monkeypatch, tmp_path, environment_variable):
    monkeypatch.setenv("CREDENTIAL_HOME", str(tmp_path))
    monkeypatch.setenv(environment_variable, "$CREDENTIAL_HOME/credentials")

    assert str(tmp_path / "credentials") in LiteLLMAIHandler._aws_credential_chain_file_paths()


def test_original_ec2_credential_file_path_does_not_expand_environment_variables(monkeypatch, tmp_path):
    monkeypatch.setenv("CREDENTIAL_HOME", str(tmp_path))
    monkeypatch.setenv("AWS_CREDENTIAL_FILE", "$CREDENTIAL_HOME/credentials")

    paths = LiteLLMAIHandler._aws_credential_chain_file_paths()

    assert os.path.abspath("$CREDENTIAL_HOME/credentials") in paths
    assert str(tmp_path / "credentials") not in paths


def test_credential_chain_file_fingerprint_skips_non_files(tmp_path):
    with patch("builtins.open") as credential_file:
        assert LiteLLMAIHandler._fingerprint_aws_credential_chain_file(str(tmp_path)) == ("nonfile",)

    credential_file.assert_not_called()


@pytest.mark.asyncio
async def test_credential_chain_file_validation_runs_off_event_loop(monkeypatch):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    handler = LiteLLMAIHandler()
    event_loop_thread = threading.get_ident()
    validation_threads = []
    monkeypatch.setattr(
        handler,
        "_validate_aws_credential_chain_environment",
        lambda: validation_threads.append(threading.get_ident()),
    )
    monkeypatch.setattr(handler, "_initialize_aws_imds_credentials", lambda: False)

    await handler._resolve_aws_request_credentials()

    assert validation_threads
    assert event_loop_thread not in validation_threads


@pytest.mark.asyncio
async def test_imds_mode_rechecks_credential_chain_file_during_initial_resolution(monkeypatch, tmp_path):
    credentials_file = tmp_path / "credentials"
    credentials_file.write_text("request-a", encoding="utf-8")
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials_file))
    credentials = MagicMock()
    credentials.get_frozen_credentials.return_value = _frozen_creds()
    session = MagicMock()

    def change_credentials_file():
        credentials_file.write_text("request-b", encoding="utf-8")
        return credentials

    session.get_credentials.side_effect = change_credentials_file
    session.region_name = "us-east-1"
    handler = LiteLLMAIHandler()

    with patch("boto3.Session", return_value=session):
        with pytest.raises(ValueError, match="Refusing changed AWS credential-chain file"):
            await _call(handler)


@pytest.mark.asyncio
async def test_imds_mode_rechecks_credential_chain_environment_before_refresh(monkeypatch):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    monkeypatch.setenv("AWS_PROFILE", "tenant-a")
    credentials = MagicMock()
    credentials.get_frozen_credentials.return_value = _frozen_creds()
    session = MagicMock()
    session.get_credentials.return_value = credentials
    session.region_name = "us-east-1"
    handler = LiteLLMAIHandler()

    with patch("boto3.Session", return_value=session) as session_factory:
        await _call(handler)
        monkeypatch.setenv("AWS_PROFILE", "tenant-b")
        with pytest.raises(ValueError, match="Refusing changed AWS credential-chain environment"):
            await _call(handler)

    session_factory.assert_called_once_with()


@pytest.mark.parametrize(
    "profiles",
    (
        {"tenant": {"credential_process": "get-credentials"}},
        {
            "tenant": {"role_arn": "arn:aws:iam::123456789012:role/tenant", "source_profile": "source"},
            "source": {"credential_process": "get-credentials"},
        },
    ),
)
@pytest.mark.asyncio
async def test_imds_mode_rejects_credential_process_profiles(monkeypatch, profiles):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    monkeypatch.setenv("AWS_PROFILE", "tenant")
    session = MagicMock()
    session.profile_name = "tenant"
    session._session.full_config = {"profiles": profiles}
    handler = LiteLLMAIHandler()

    with patch("boto3.Session", return_value=session):
        with pytest.raises(ValueError, match="credential_process"):
            await _call(handler)

    session.get_credentials.assert_not_called()


@pytest.mark.asyncio
async def test_imds_mode_uses_static_fallback_for_credential_process_profile(monkeypatch):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    monkeypatch.setenv("AWS_PROFILE", "tenant")
    monkeypatch.setattr(litellm_handler, "get_settings", _static_aws_settings)
    session = MagicMock()
    session.profile_name = "tenant"
    session._session.full_config = {
        "profiles": {"tenant": {"credential_process": "get-credentials"}},
    }
    handler = LiteLLMAIHandler()

    with patch("boto3.Session", return_value=session):
        await _call(handler)

    session.get_credentials.assert_not_called()
    assert handler._aws_imds_mode is False
    assert handler._aws_imds_fell_back is True
    assert handler._aws_active_creds["aws_access_key_id"] == "STATIC-KEY"


@pytest.mark.asyncio
async def test_imds_mode_rejects_credential_chain_change_during_initial_resolution(monkeypatch):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    credentials = MagicMock()
    credentials.get_frozen_credentials.return_value = _frozen_creds()
    session = MagicMock()

    def change_environment():
        monkeypatch.setenv("AWS_PROFILE", "another-request")
        return credentials

    session.get_credentials.side_effect = change_environment
    session.region_name = "us-east-1"
    handler = LiteLLMAIHandler()

    with patch("boto3.Session", return_value=session):
        with pytest.raises(ValueError, match="Refusing changed AWS credential-chain environment"):
            await _call(handler)


@pytest.mark.asyncio
async def test_imds_mode_rejects_credential_chain_change_during_refresh(monkeypatch):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    credentials = MagicMock()
    credentials.get_frozen_credentials.return_value = _frozen_creds()
    session = MagicMock()
    session.get_credentials.return_value = credentials
    session.region_name = "us-east-1"
    handler = LiteLLMAIHandler()

    with patch("boto3.Session", return_value=session):
        await _call(handler)

        def change_environment():
            monkeypatch.setenv("AWS_PROFILE", "another-request")
            return _frozen_creds("ANOTHER-KEY", "ANOTHER-SECRET")

        credentials.get_frozen_credentials.side_effect = change_environment
        with pytest.raises(ValueError, match="Refusing changed AWS credential-chain environment"):
            await _call(handler)


@pytest.mark.asyncio
async def test_ambient_region_precedes_configured_and_session_region(monkeypatch):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    monkeypatch.setenv("AWS_REGION_NAME", "us-west-2")
    monkeypatch.setattr(litellm_handler, "get_settings", _static_aws_settings)
    session = MagicMock()
    session.get_credentials.return_value.get_frozen_credentials.return_value = _frozen_creds()
    session.region_name = "eu-west-1"

    with patch("boto3.Session", return_value=session):
        handler = LiteLLMAIHandler()
        await _call(handler)

    assert handler._aws_active_creds["aws_region_name"] == "us-west-2"
    assert os.environ["AWS_REGION_NAME"] == "us-west-2"


def test_no_imds_lookup_when_disabled():
    with patch("boto3.Session") as session:
        LiteLLMAIHandler()

    session.assert_not_called()


@pytest.mark.parametrize("error", (
    CredentialRetrievalError(provider="imds", error_msg="unreachable"),
    ClientError(
        error_response={"Error": {"Code": "AccessDenied", "Message": "denied"}},
        operation_name="AssumeRole",
    ),
))
@pytest.mark.asyncio
async def test_imds_resolution_failure_uses_request_local_static_fallback(monkeypatch, error):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    monkeypatch.setattr(litellm_handler, "get_settings", _static_aws_settings)
    session = MagicMock()
    session.get_credentials.side_effect = error
    session.region_name = None

    with patch("boto3.Session", return_value=session):
        handler = LiteLLMAIHandler()
        await _call(handler)

    assert handler._aws_imds_mode is False
    assert handler._aws_active_creds["aws_access_key_id"] == "STATIC-KEY"
    assert handler._aws_active_creds["aws_session_token"] == ""
    assert "AWS_ACCESS_KEY_ID" not in os.environ


@pytest.mark.parametrize(
    ("model", "environment_variable"),
    (
        ("bedrock/anthropic.claude-sonnet-4-5", "AWS_ENDPOINT_URL_BEDROCK_RUNTIME"),
        ("sagemaker/my-endpoint", "AWS_ENDPOINT_URL_SAGEMAKER_RUNTIME"),
    ),
)
@pytest.mark.asyncio
async def test_static_fallback_rejects_late_aws_request_endpoint_environment(
    monkeypatch,
    model,
    environment_variable,
):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    monkeypatch.setattr(litellm_handler, "get_settings", _static_aws_settings)
    session = MagicMock()
    session.get_credentials.side_effect = CredentialRetrievalError(provider="imds", error_msg="unreachable")
    session.region_name = None

    with patch("boto3.Session", return_value=session):
        handler = LiteLLMAIHandler()
        await _call(handler, model=model)
        monkeypatch.setenv(environment_variable, "https://another-request.example.com")

        with pytest.raises(ValueError, match="Refusing changed AWS request endpoint environment"):
            await _call(handler, model=model)


@pytest.mark.asyncio
async def test_imds_session_creation_failure_uses_request_local_static_fallback(monkeypatch):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    monkeypatch.setattr(litellm_handler, "get_settings", _static_aws_settings)

    with patch("boto3.Session", side_effect=ProfileNotFound(profile="missing")):
        handler = LiteLLMAIHandler()
        kwargs = await _call(handler)

    assert handler._aws_imds_mode is False
    assert kwargs["aws_access_key_id"] == "STATIC-KEY"
    assert kwargs["aws_secret_access_key"] == "STATIC-SECRET"
    assert kwargs["aws_region_name"] == "us-east-1"
    assert kwargs["aws_session_token"] == ""
    assert handler._aws_imds_fell_back is True
    assert "AWS_ACCESS_KEY_ID" not in os.environ


@pytest.mark.asyncio
async def test_imds_resolution_without_static_credentials_retries_on_next_request(monkeypatch):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    first_session = MagicMock()
    first_session.get_credentials.return_value = None
    second_session = MagicMock()
    second_session.get_credentials.return_value.get_frozen_credentials.return_value = _frozen_creds()

    with patch("boto3.Session", side_effect=(first_session, second_session)) as session_factory:
        handler = LiteLLMAIHandler()
        with pytest.raises(ValueError, match="AWS credentials were not resolved for this request"):
            await _call(handler)
        assert handler._aws_imds_initialized is False

        kwargs = await _call(handler)

    assert session_factory.call_count == 2
    assert handler._aws_imds_initialized is True
    assert kwargs["aws_access_key_id"] == "IMDS-KEY"
    assert kwargs["aws_secret_access_key"] == "IMDS-SECRET"
    assert kwargs["aws_region_name"] == "us-east-1"


@pytest.mark.asyncio
async def test_imds_credential_snapshot_oserror_uses_request_local_static_fallback(monkeypatch):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    monkeypatch.setattr(litellm_handler, "get_settings", _static_aws_settings)
    session = MagicMock()
    session.get_credentials.return_value.get_frozen_credentials.side_effect = OSError("token file unavailable")
    session.region_name = "us-east-1"

    with patch("boto3.Session", return_value=session):
        handler = LiteLLMAIHandler()
        kwargs = await _call(handler)

    assert handler._aws_imds_mode is False
    assert kwargs["aws_access_key_id"] == "STATIC-KEY"
    assert kwargs["aws_secret_access_key"] == "STATIC-SECRET"
    assert kwargs["aws_region_name"] == "us-east-1"
    assert kwargs["aws_session_token"] == ""
    assert handler._aws_imds_fell_back is True
    assert "AWS_ACCESS_KEY_ID" not in os.environ


@pytest.mark.asyncio
async def test_imds_refresh_updates_request_credentials(monkeypatch):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    credentials = MagicMock()
    credentials.get_frozen_credentials.side_effect = (
        _frozen_creds(),
        _frozen_creds("ROTATED-KEY", "ROTATED-SECRET", "ROTATED-TOKEN"),
    )
    session = MagicMock()
    session.get_credentials.return_value = credentials
    session.region_name = "us-east-1"

    with patch("boto3.Session", return_value=session):
        handler = LiteLLMAIHandler()
        await _call(handler)

    assert handler._aws_active_creds["aws_access_key_id"] == "ROTATED-KEY"
    assert handler._aws_active_creds["aws_session_token"] == "ROTATED-TOKEN"
    assert "AWS_ACCESS_KEY_ID" not in os.environ


@pytest.mark.asyncio
async def test_bedrock_call_refreshes_and_forwards_imds_credentials(monkeypatch):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    credentials = MagicMock()
    credentials.get_frozen_credentials.side_effect = (
        _frozen_creds(),
        _frozen_creds("ROTATED-KEY", "ROTATED-SECRET"),
    )
    session = MagicMock()
    session.get_credentials.return_value = credentials
    session.region_name = "us-east-1"

    with patch("boto3.Session", return_value=session):
        handler = LiteLLMAIHandler()
        kwargs = await _call(handler)

    assert kwargs["aws_access_key_id"] == "ROTATED-KEY"
    assert kwargs["aws_secret_access_key"] == "ROTATED-SECRET"
    assert kwargs["aws_session_token"] == ""


@pytest.mark.asyncio
async def test_imds_refresh_lock_does_not_serialize_network_calls(monkeypatch):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    credentials = MagicMock()
    credentials.get_frozen_credentials.return_value = _frozen_creds()
    session = MagicMock()
    session.get_credentials.return_value = credentials
    session.region_name = "us-east-1"
    both_started = asyncio.Event()
    started = 0

    async def capture_call(**kwargs):
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=5)
        return _mock_response()

    with patch("boto3.Session", return_value=session):
        handler = LiteLLMAIHandler()
        with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", side_effect=capture_call):
            await asyncio.gather(
                handler.chat_completion(model="bedrock/anthropic.claude-sonnet-4-5", system="sys", user="one"),
                handler.chat_completion(model="bedrock/anthropic.claude-sonnet-4-5", system="sys", user="two"),
            )

    assert started == 2


@pytest.mark.parametrize("initialized", (False, True), ids=("initialize", "refresh"))
@pytest.mark.asyncio
async def test_cancelled_aws_snapshot_holds_lock_until_worker_finishes(monkeypatch, initialized):
    handler = LiteLLMAIHandler()
    handler._aws_use_imds = True
    handler._aws_imds_initialized = initialized
    handler._aws_imds_mode = initialized
    started = threading.Event()
    release = threading.Event()

    def change_state():
        started.set()
        release.wait(timeout=5)
        handler._aws_active_creds = {"aws_access_key_id": "worker-key"}
        handler._aws_imds_mode = True
        return True

    callback_name = "_refresh_aws_imds_credentials" if initialized else "_initialize_aws_imds_credentials"
    monkeypatch.setattr(handler, callback_name, change_state)

    async def wait_until_started():
        while not started.is_set():
            await asyncio.sleep(0)

    async def wait_until_snapshot_cleanup():
        while handler._aws_snapshot_tasks:
            await asyncio.sleep(0)

    async def snapshot_credentials():
        async with handler._snapshot_aws_request_credentials(True):
            pass

    first = asyncio.create_task(snapshot_credentials())
    await asyncio.wait_for(wait_until_started(), timeout=5)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(first, timeout=1)

    second_acquired = asyncio.Event()

    async def acquire_after_worker():
        async with handler._aws_bedrock_lock:
            second_acquired.set()

    second = asyncio.create_task(acquire_after_worker())
    await asyncio.sleep(0)
    assert handler._aws_bedrock_lock.locked()
    assert not second_acquired.is_set()

    release.set()
    await asyncio.wait_for(second, timeout=1)

    assert second_acquired.is_set()
    assert handler._aws_active_creds == {"aws_access_key_id": "worker-key"}
    await asyncio.wait_for(wait_until_snapshot_cleanup(), timeout=1)
    assert not handler._aws_snapshot_tasks


@pytest.mark.parametrize(
    "error",
    (
        CredentialRetrievalError(provider="imds", error_msg="expired"),
        OSError("token file unavailable"),
    ),
)
@pytest.mark.asyncio
async def test_refresh_failure_activates_static_fallback_before_call(monkeypatch, error):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    monkeypatch.setattr(litellm_handler, "get_settings", _static_aws_settings)
    credentials = MagicMock()
    credentials.get_frozen_credentials.side_effect = (
        _frozen_creds(),
        error,
    )
    session = MagicMock()
    session.get_credentials.return_value = credentials
    session.region_name = "us-east-1"

    with patch("boto3.Session", return_value=session):
        handler = LiteLLMAIHandler()
        kwargs = await _call(handler)

    assert kwargs["aws_access_key_id"] == "STATIC-KEY"
    assert kwargs["aws_session_token"] == ""
    assert handler._aws_imds_fell_back is True


@pytest.mark.asyncio
async def test_bedrock_api_error_retries_with_static_request_credentials(monkeypatch):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    monkeypatch.setenv("AWS_REGION_NAME", "eu-west-1")
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _static_aws_settings(session_token="STATIC-TOKEN"),
    )
    credentials = MagicMock()
    credentials.get_frozen_credentials.return_value = _frozen_creds(token="IMDS-TOKEN")
    session = MagicMock()
    session.get_credentials.return_value = credentials
    session.region_name = "us-east-1"
    calls = []

    async def fail_then_succeed(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise openai.AuthenticationError(
                "Bedrock auth failed",
                response=httpx.Response(401, request=httpx.Request("POST", "https://bedrock.example")),
                body=None,
            )
        return _mock_response()

    with patch("boto3.Session", return_value=session):
        handler = LiteLLMAIHandler()
        with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", side_effect=fail_then_succeed):
            await handler.chat_completion(model="bedrock/anthropic.claude-sonnet-4-5", system="sys", user="usr")

    assert calls[0]["aws_access_key_id"] == "IMDS-KEY"
    assert calls[0]["aws_secret_access_key"] == "IMDS-SECRET"
    assert calls[0]["aws_session_token"] == "IMDS-TOKEN"
    assert calls[0]["aws_region_name"] == "eu-west-1"
    assert calls[1]["aws_access_key_id"] == "STATIC-KEY"
    assert calls[1]["aws_secret_access_key"] == "STATIC-SECRET"
    assert calls[1]["aws_session_token"] == "STATIC-TOKEN"
    assert calls[1]["aws_region_name"] == "us-east-1"
    assert handler._aws_imds_fell_back is True


@pytest.mark.asyncio
async def test_health_probe_retries_with_static_request_credentials(monkeypatch):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    monkeypatch.setenv("AWS_REGION_NAME", "eu-west-1")
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _static_aws_settings(session_token="STATIC-TOKEN"),
    )
    credentials = MagicMock()
    credentials.get_frozen_credentials.return_value = _frozen_creds(token="IMDS-TOKEN")
    session = MagicMock()
    session.get_credentials.return_value = credentials
    session.region_name = "us-east-1"
    calls = []

    async def fail_then_succeed(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise openai.AuthenticationError(
                "Bedrock auth failed",
                response=httpx.Response(401, request=httpx.Request("POST", "https://bedrock.example")),
                body=None,
            )
        return _mock_response()

    with patch("boto3.Session", return_value=session):
        handler = LiteLLMAIHandler()
        await handler.probe_completion(
            model="bedrock/anthropic.claude-sonnet-4-5",
            _completion=fail_then_succeed,
        )

    assert calls[0]["aws_access_key_id"] == "IMDS-KEY"
    assert calls[0]["aws_secret_access_key"] == "IMDS-SECRET"
    assert calls[0]["aws_session_token"] == "IMDS-TOKEN"
    assert calls[0]["aws_region_name"] == "eu-west-1"
    assert calls[1]["aws_access_key_id"] == "STATIC-KEY"
    assert calls[1]["aws_secret_access_key"] == "STATIC-SECRET"
    assert calls[1]["aws_session_token"] == "STATIC-TOKEN"
    assert calls[1]["aws_region_name"] == "us-east-1"
    assert handler._aws_imds_fell_back is True


@pytest.mark.parametrize("probe", (False, True))
@pytest.mark.asyncio
async def test_bedrock_mantle_static_fallback_preserves_request_region(monkeypatch, probe):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    monkeypatch.setenv("AWS_REGION_NAME", "eu-west-1")
    monkeypatch.setenv("BEDROCK_MANTLE_API_BASE", "https://bedrock-mantle.eu-west-1.api.aws/v1")
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _static_aws_settings(session_token="STATIC-TOKEN"),
    )
    credentials = MagicMock()
    credentials.get_frozen_credentials.return_value = _frozen_creds(token="IMDS-TOKEN")
    session = MagicMock()
    session.get_credentials.return_value = credentials
    session.region_name = "eu-west-1"
    calls = []

    async def fail_then_succeed(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise openai.AuthenticationError(
                "Bedrock auth failed",
                response=httpx.Response(401, request=httpx.Request("POST", "https://bedrock.example")),
                body=None,
            )
        return _mock_response()

    with patch("boto3.Session", return_value=session):
        handler = LiteLLMAIHandler()
        if probe:
            await handler.probe_completion(
                model="bedrock_mantle/openai.gpt-oss-120b",
                _completion=fail_then_succeed,
            )
        else:
            with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", side_effect=fail_then_succeed):
                await handler.chat_completion(
                    model="bedrock_mantle/openai.gpt-oss-120b",
                    system="sys",
                    user="usr",
                )

    assert calls[0]["aws_access_key_id"] == "IMDS-KEY"
    assert calls[0]["aws_region_name"] == "eu-west-1"
    assert calls[1]["aws_access_key_id"] == "STATIC-KEY"
    assert calls[1]["aws_region_name"] == "eu-west-1"
    assert handler._aws_imds_fell_back is True


@pytest.mark.asyncio
async def test_health_probe_retries_generic_expired_token_error(monkeypatch):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    monkeypatch.setattr(litellm_handler, "get_settings", _static_aws_settings)
    credentials = MagicMock()
    credentials.get_frozen_credentials.return_value = _frozen_creds()
    session = MagicMock()
    session.get_credentials.return_value = credentials
    session.region_name = "us-east-1"
    completion = AsyncMock(
        side_effect=(
            openai.APIConnectionError(
                message="Bedrock - ExpiredTokenException: The security token has expired",
                request=httpx.Request("POST", "https://bedrock.example"),
            ),
            _mock_response(),
        )
    )

    with patch("boto3.Session", return_value=session):
        handler = LiteLLMAIHandler()
        await handler.probe_completion(
            model="bedrock/anthropic.claude-sonnet-4-5",
            _completion=completion,
        )

    assert completion.await_count == 2
    assert completion.await_args_list[0].kwargs["aws_access_key_id"] == "IMDS-KEY"
    assert completion.await_args_list[1].kwargs["aws_access_key_id"] == "STATIC-KEY"
    assert handler._aws_imds_fell_back is True


@pytest.mark.parametrize(
    ("model", "error"),
    (
        (
            "bedrock/anthropic.claude-sonnet-4-5",
            openai.APIConnectionError(
                message="Bedrock - ExpiredTokenException: The security token has expired",
                request=httpx.Request("POST", "https://bedrock.example"),
            ),
        ),
        (
            "sagemaker_chat/model",
            openai.BadRequestError(
                "SagemakerException - Unable to locate credentials",
                response=httpx.Response(400, request=httpx.Request("POST", "https://sagemaker.example")),
                body=None,
            ),
        ),
        (
            "bedrock/anthropic.claude-sonnet-4-5",
            openai.PermissionDeniedError(
                "Bedrock - ExpiredTokenException: The security token has expired",
                response=httpx.Response(403, request=httpx.Request("POST", "https://bedrock.example")),
                body=None,
            ),
        ),
    ),
)
@pytest.mark.asyncio
async def test_chat_completion_retries_mapped_aws_credential_errors(monkeypatch, model, error):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    monkeypatch.setattr(litellm_handler, "get_settings", _static_aws_settings)
    credentials = MagicMock()
    credentials.get_frozen_credentials.return_value = _frozen_creds()
    session = MagicMock()
    session.get_credentials.return_value = credentials
    session.region_name = "us-east-1"
    completion = AsyncMock(side_effect=(error, _mock_response()))

    with patch("boto3.Session", return_value=session):
        handler = LiteLLMAIHandler()
        with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", completion):
            await handler.chat_completion(model=model, system="sys", user="usr")

    assert completion.await_count == 2
    assert completion.await_args_list[0].kwargs["aws_access_key_id"] == "IMDS-KEY"
    assert completion.await_args_list[1].kwargs["aws_access_key_id"] == "STATIC-KEY"
    assert handler._aws_imds_fell_back is True


@pytest.mark.parametrize(
    "error",
    (
        openai.RateLimitError(
            "rate limited",
            response=httpx.Response(429, request=httpx.Request("POST", "https://bedrock.example")),
            body=None,
        ),
        openai.APITimeoutError(request=httpx.Request("POST", "https://bedrock.example")),
        openai.PermissionDeniedError(
            "Bedrock - AccessDeniedException: User is not authorized to perform this action",
            response=httpx.Response(403, request=httpx.Request("POST", "https://bedrock.example")),
            body=None,
        ),
    ),
)
@pytest.mark.asyncio
async def test_health_probe_does_not_retry_non_authentication_errors(monkeypatch, error):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    monkeypatch.setattr(litellm_handler, "get_settings", _static_aws_settings)
    credentials = MagicMock()
    credentials.get_frozen_credentials.return_value = _frozen_creds()
    session = MagicMock()
    session.get_credentials.return_value = credentials
    session.region_name = "us-east-1"
    completion = AsyncMock(side_effect=error)

    with patch("boto3.Session", return_value=session):
        handler = LiteLLMAIHandler()
        with pytest.raises(type(error)):
            await handler.probe_completion(
                model="bedrock/anthropic.claude-sonnet-4-5",
                _completion=completion,
            )

    completion.assert_awaited_once()
    assert handler._aws_imds_fell_back is False


@pytest.mark.parametrize(
    "error",
    (
        openai.APITimeoutError(request=httpx.Request("POST", "https://bedrock.example")),
        openai.PermissionDeniedError(
            "Bedrock - AccessDeniedException: User is not authorized to perform this action",
            response=httpx.Response(403, request=httpx.Request("POST", "https://bedrock.example")),
            body=None,
        ),
    ),
)
@pytest.mark.asyncio
async def test_chat_completion_does_not_fall_back_on_noncredential_errors(monkeypatch, error):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    monkeypatch.setattr(litellm_handler, "get_settings", _static_aws_settings)
    credentials = MagicMock()
    credentials.get_frozen_credentials.return_value = _frozen_creds()
    session = MagicMock()
    session.get_credentials.return_value = credentials
    session.region_name = "us-east-1"
    completion = AsyncMock(side_effect=error)

    with patch("boto3.Session", return_value=session):
        handler = LiteLLMAIHandler()
        with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", completion):
            with pytest.raises(type(error)):
                await handler.chat_completion(
                    model="bedrock/anthropic.claude-sonnet-4-5",
                    system="sys",
                    user="usr",
                )

    assert completion.await_count == litellm_handler.MODEL_RETRIES
    assert all(call.kwargs["aws_access_key_id"] == "IMDS-KEY" for call in completion.await_args_list)
    assert handler._aws_imds_fell_back is False


@pytest.mark.asyncio
async def test_non_bedrock_call_does_not_receive_aws_credentials(monkeypatch):
    monkeypatch.setattr(litellm_handler, "get_settings", _static_aws_settings)

    kwargs = await _call(LiteLLMAIHandler(), model="groq/llama-3.3-70b-versatile")

    assert not any(key.startswith("aws_") for key in kwargs)


@pytest.mark.asyncio
async def test_concurrent_handlers_keep_static_aws_credentials_isolated(monkeypatch):
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _static_aws_settings("TENANT-A"))
    tenant_a = LiteLLMAIHandler()
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _static_aws_settings("TENANT-B"))
    tenant_b = LiteLLMAIHandler()
    calls = []

    async def capture_call(**kwargs):
        await asyncio.sleep(0)
        calls.append(kwargs)
        return _mock_response()

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", side_effect=capture_call):
        await asyncio.gather(
            tenant_a.chat_completion(model="bedrock/anthropic.claude-sonnet-4-5", system="sys", user="usr"),
            tenant_b.chat_completion(model="bedrock/anthropic.claude-sonnet-4-5", system="sys", user="usr"),
        )

    access_keys = {call["aws_access_key_id"] for call in calls}
    assert access_keys == {"TENANT-A-KEY", "TENANT-B-KEY"}
    assert "AWS_ACCESS_KEY_ID" not in os.environ
