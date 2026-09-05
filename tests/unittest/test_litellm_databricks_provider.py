"""Tests for request-local Databricks provider wiring."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import litellm
import pytest

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
    for variable in ("DATABRICKS_API_KEY", "DATABRICKS_API_BASE", "AWS_USE_IMDS", "OPENAI_API_KEY"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(litellm, "api_key", None)
    monkeypatch.setattr(litellm, "openai_key", None)


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
