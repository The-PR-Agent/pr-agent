"""Tests for request-local Databricks provider wiring."""

import asyncio
import atexit
import os
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import litellm
import pytest
import requests
from litellm.llms.databricks.common_utils import DatabricksBase

import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_handler


def _make_settings(overrides):
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


def _mock_response():
    mock = MagicMock()
    mock.__getitem__ = lambda self, key: {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]
    }[key]
    mock.dict.return_value = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
    return mock


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch):
    for variable in (
        "DATABRICKS_API_KEY", "DATABRICKS_API_BASE", "DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET",
        "AWS_USE_IMDS", "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(litellm, "api_key", None)
    monkeypatch.setattr(litellm, "openai_key", None)
    monkeypatch.setattr(litellm, "databricks_key", None)
    validator = DatabricksBase.databricks_validate_environment
    monkeypatch.setattr(
        DatabricksBase, "databricks_validate_environment",
        getattr(validator, "_pr_agent_original_databricks_validate_environment", validator),
    )


@pytest.mark.asyncio
async def test_databricks_settings_are_forwarded_without_exporting_env(monkeypatch):
    overrides = {
        "DATABRICKS.API_KEY": "dapi-test-123",
        "DATABRICKS.API_BASE": "https://adb-1234.azuredatabricks.net/serving-endpoints",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()
        await handler.chat_completion(model="databricks/endpoint", system="sys", user="usr")

    assert mock_call.call_args.kwargs["api_key"] == "dapi-test-123"
    assert mock_call.call_args.kwargs["api_base"] == overrides["DATABRICKS.API_BASE"]
    assert "DATABRICKS_API_KEY" not in os.environ
    assert "DATABRICKS_API_BASE" not in os.environ


@pytest.mark.asyncio
async def test_databricks_api_base_is_optional(monkeypatch):
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings({"DATABRICKS.API_KEY": "dapi-only-key"}),
    )

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()
        await handler.chat_completion(model="databricks/endpoint", system="sys", user="usr")

    assert mock_call.call_args.kwargs["api_key"] == "dapi-only-key"
    assert "api_base" not in mock_call.call_args.kwargs


@pytest.mark.asyncio
async def test_databricks_native_env_is_forwarded_without_openai_gateway(monkeypatch):
    monkeypatch.setenv("DATABRICKS_API_KEY", "native-databricks-key")
    monkeypatch.setenv("DATABRICKS_API_BASE", "https://databricks.example")
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings({"OPENAI.API_BASE": "https://gateway.example/v1"}),
    )

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()
        await handler.chat_completion(model="databricks/endpoint", system="sys", user="usr")

    assert mock_call.call_args.kwargs["api_key"] == "native-databricks-key"
    assert mock_call.call_args.kwargs["api_base"] == "https://databricks.example"


@pytest.mark.asyncio
async def test_databricks_native_endpoint_is_frozen_with_its_key(monkeypatch):
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings({}))
    monkeypatch.setenv("DATABRICKS_API_KEY", "native-databricks-key")
    monkeypatch.setenv("DATABRICKS_API_BASE", "https://tenant-a.example")
    handler = litellm_handler.LiteLLMAIHandler()
    monkeypatch.setenv("DATABRICKS_API_KEY", "another-request-key")
    monkeypatch.setenv("DATABRICKS_API_BASE", "https://tenant-b.example")

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        await handler.chat_completion(model="databricks/endpoint", system="sys", user="usr")

    assert mock_call.call_args.kwargs["api_key"] == "native-databricks-key"
    assert mock_call.call_args.kwargs["api_base"] == "https://tenant-a.example"


@pytest.mark.asyncio
async def test_databricks_does_not_receive_another_provider_credentials(monkeypatch):
    overrides = {
        "OPENROUTER.KEY": "openrouter-key",
        "OPENROUTER.API_BASE": "https://openrouter.example/v1",
    }
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(overrides))

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()
        await handler.chat_completion(model="databricks/endpoint", system="sys", user="usr")

    assert "api_key" not in mock_call.call_args.kwargs
    assert "api_base" not in mock_call.call_args.kwargs


@pytest.mark.parametrize("entrypoint", ("chat_completion", "probe_completion"))
@pytest.mark.parametrize("key_source", ("global_before", "global_after", "provider_global", "captured_pat", "literal_dummy"))
@pytest.mark.asyncio
async def test_databricks_native_oauth_is_not_overwritten_by_foreign_keys(monkeypatch, entrypoint, key_source):
    from litellm.litellm_core_utils import logging_worker

    endpoint = "https://databricks.example/serving-endpoints"
    monkeypatch.setenv("DATABRICKS_API_BASE", endpoint)
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "request-client")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "request-secret")
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings({}))
    expected_token = "request-oauth-token"
    if key_source in ("captured_pat", "literal_dummy"):
        expected_token = "request-pat" if key_source == "captured_pat" else litellm_handler.DUMMY_LITELLM_API_KEY
        monkeypatch.setenv("DATABRICKS_API_KEY", expected_token)
    if key_source == "global_before":
        monkeypatch.setattr(litellm, "api_key", "foreign-global-key")
    if key_source == "provider_global":
        monkeypatch.setattr(litellm, "databricks_key", "foreign-provider-key")
    handler = litellm_handler.LiteLLMAIHandler()
    if key_source in ("global_after", "captured_pat", "literal_dummy"):
        monkeypatch.setattr(litellm, "api_key", "foreign-global-key")
        monkeypatch.setenv("DATABRICKS_API_KEY", "late-pat")
    oauth_calls, inference = [], []

    def oauth_post(url, **kwargs):
        assert url == "https://databricks.example/oidc/v1/token"
        assert kwargs["auth"] == ("request-client", "request-secret")
        oauth_calls.append(url)
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"access_token": "request-oauth-token"}'
        return response

    async def send(client, request, **kwargs):
        assert str(request.url) == endpoint + "/chat/completions"
        inference.append(request.headers.get("authorization"))
        return httpx.Response(200, request=request, json={
            "id": "test", "object": "chat.completion", "created": 0, "model": "endpoint",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    monkeypatch.setattr(requests, "post", oauth_post)
    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    worker = logging_worker.LoggingWorker()
    atexit.unregister(worker._flush_on_exit)
    monkeypatch.setattr(logging_worker, "GLOBAL_LOGGING_WORKER", worker)
    try:
        if entrypoint == "chat_completion":
            assert await handler.chat_completion("databricks/endpoint", "sys", "usr") == ("ok", "stop")
        else:
            await handler.probe_completion("databricks/endpoint")
    finally:
        await asyncio.sleep(0)
        await worker.flush()
        await worker.stop()
    assert len(oauth_calls) == 1
    assert inference == [f"Bearer {expected_token}"]


def _native_validation(api_key, api_base="https://databricks.example/serving-endpoints", positional=False):
    validator = DatabricksBase().databricks_validate_environment
    if positional:
        return validator(api_key, api_base, "chat_completions", False, None)
    return validator(
        api_key=api_key, api_base=api_base, endpoint_type="chat_completions", custom_endpoint=False, headers=None,
    )


@pytest.fixture
def native_oauth(monkeypatch):
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "request-client")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "request-secret")
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings({}))
    token = MagicMock(return_value="request-oauth-token")
    monkeypatch.setattr(DatabricksBase, "_get_oauth_m2m_token", token)
    return token


@pytest.mark.parametrize("positional", (False, True))
@pytest.mark.asyncio
async def test_databricks_bridge_only_translates_scoped_placeholder(native_oauth, positional):
    handler = litellm_handler.LiteLLMAIHandler()
    dummy = litellm_handler.DUMMY_LITELLM_API_KEY

    async def completion(**kwargs):
        assert litellm_handler._databricks_request_keyless.get() is True
        assert _native_validation("actual-pat", positional=positional)[1]["Authorization"] == "Bearer actual-pat"
        return _native_validation(kwargs["api_key"], positional=positional)

    result = await handler._acompletion(_completion=completion, model="databricks/endpoint", api_key=dummy)
    assert result[1]["Authorization"] == "Bearer request-oauth-token"
    assert litellm_handler._databricks_request_keyless.get() is False
    assert _native_validation(dummy, positional=positional)[1]["Authorization"] == f"Bearer {dummy}"
    installed = DatabricksBase.databricks_validate_environment
    litellm_handler._install_databricks_keyless_bridge()
    assert DatabricksBase.databricks_validate_environment is installed


@pytest.mark.asyncio
async def test_databricks_concurrent_and_nested_literal_pat_keeps_context(native_oauth, monkeypatch):
    keyless = litellm_handler.LiteLLMAIHandler()
    dummy = litellm_handler.DUMMY_LITELLM_API_KEY
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings({"DATABRICKS.API_KEY": dummy}))
    pat = litellm_handler.LiteLLMAIHandler()
    entered, release = asyncio.Event(), asyncio.Event()

    async def pat_completion(**kwargs):
        assert litellm_handler._databricks_request_keyless.get() is False
        return _native_validation(kwargs["api_key"])[1]["Authorization"]

    async def keyless_completion(**kwargs):
        entered.set()
        await release.wait()
        assert await pat._acompletion(
            _completion=pat_completion, model="databricks/endpoint", api_key=dummy,
        ) == f"Bearer {dummy}"
        assert litellm_handler._databricks_request_keyless.get() is True
        return _native_validation(kwargs["api_key"])[1]["Authorization"]

    task = asyncio.create_task(keyless._acompletion(
        _completion=keyless_completion, model="databricks/endpoint", api_key=dummy,
    ))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert litellm_handler._databricks_request_keyless.get() is False
        assert _native_validation(dummy)[1]["Authorization"] == f"Bearer {dummy}"
        assert await pat._acompletion(
            _completion=pat_completion, model="databricks/endpoint", api_key=dummy,
        ) == f"Bearer {dummy}"
    finally:
        release.set()
        result = await asyncio.wait_for(task, timeout=5)
    assert result == "Bearer request-oauth-token"
    assert litellm_handler._databricks_request_keyless.get() is False


@pytest.mark.parametrize("cancel", (False, True), ids=("failure", "cancellation"))
@pytest.mark.asyncio
async def test_databricks_context_resets_after_interrupted_completion(native_oauth, cancel):
    handler = litellm_handler.LiteLLMAIHandler()
    dummy = litellm_handler.DUMMY_LITELLM_API_KEY
    entered = asyncio.Event()

    async def completion(**kwargs):
        assert _native_validation(kwargs["api_key"])[1]["Authorization"] == "Bearer request-oauth-token"
        entered.set()
        if cancel:
            await asyncio.Future()
        raise RuntimeError("completion failed")

    async def request():
        try:
            await handler._acompletion(_completion=completion, model="databricks/endpoint", api_key=dummy)
        finally:
            # Check the task that owned the context, not only its unaffected parent.
            assert litellm_handler._databricks_request_keyless.get() is False
            assert _native_validation(dummy)[1]["Authorization"] == f"Bearer {dummy}"

    task = asyncio.create_task(request())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
    finally:
        if cancel or not entered.is_set():
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await asyncio.wait_for(task, timeout=5)
    assert litellm_handler._databricks_request_keyless.get() is False


@pytest.mark.parametrize("interface", (None, lambda self: None, lambda self, api_key, /: None),
                         ids=("missing", "missing-api-key", "positional-only"))
@pytest.mark.asyncio
async def test_databricks_incompatible_interface_fails_before_completion(monkeypatch, interface):
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings({}))
    handler = litellm_handler.LiteLLMAIHandler()
    monkeypatch.setattr(DatabricksBase, "databricks_validate_environment", interface)
    completion = AsyncMock()
    with pytest.raises(RuntimeError, match="Databricks authentication interface is incompatible"):
        await handler._acompletion(
            _completion=completion, model="databricks/endpoint", api_key=litellm_handler.DUMMY_LITELLM_API_KEY,
        )
    completion.assert_not_awaited()
    assert litellm_handler._databricks_request_keyless.get() is False
    await handler._acompletion(_completion=completion, model="databricks/endpoint", api_key="actual-pat")
    completion.assert_awaited_once()


@pytest.mark.parametrize("api_base", (None, "https://captured-endpoint.example/serving-endpoints"))
@pytest.mark.asyncio
async def test_databricks_optional_sdk_boundary_preserves_native_endpoint_contract(monkeypatch, api_base):
    # Exercise LiteLLM's optional SDK boundary, not any installed SDK implementation.
    sdk = ModuleType("databricks.sdk")
    authenticate = MagicMock(return_value={"Authorization": "Bearer sdk-token"})
    sdk.WorkspaceClient = MagicMock(return_value=SimpleNamespace(config=SimpleNamespace(
        host="https://sdk-host.example", authenticate=authenticate,
    )))
    sdk.useragent = SimpleNamespace(with_partner=MagicMock())
    package = ModuleType("databricks")
    package.__path__ = []
    package.sdk = sdk
    monkeypatch.setitem(sys.modules, "databricks", package)
    monkeypatch.setitem(sys.modules, "databricks.sdk", sdk)
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings({}))
    handler = litellm_handler.LiteLLMAIHandler()
    monkeypatch.setattr(litellm, "api_key", "foreign-global-key")
    monkeypatch.setattr(litellm, "databricks_key", "foreign-provider-key")
    monkeypatch.setenv("DATABRICKS_API_KEY", "late-pat")
    dummy = litellm_handler.DUMMY_LITELLM_API_KEY

    async def completion(**kwargs):
        return _native_validation(kwargs["api_key"], api_base=api_base)

    endpoint, headers = await handler._acompletion(
        _completion=completion, model="databricks/endpoint", api_key=dummy,
    )
    assert endpoint == (api_base or "https://sdk-host.example/serving-endpoints") + "/chat/completions"
    assert headers["Authorization"] == "Bearer sdk-token"
    sdk.WorkspaceClient.assert_called_once_with()
    sdk.useragent.with_partner.assert_called_once_with("litellm")
    authenticate.assert_called_once_with()
    assert litellm.api_key == "foreign-global-key"
    assert litellm.databricks_key == "foreign-provider-key"
    assert os.environ["DATABRICKS_API_KEY"] == "late-pat"
    assert litellm_handler._databricks_request_keyless.get() is False
    assert _native_validation(dummy)[1]["Authorization"] == f"Bearer {dummy}"
    authenticate.assert_called_once_with()
