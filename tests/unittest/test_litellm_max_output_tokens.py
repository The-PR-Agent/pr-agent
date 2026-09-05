"""
Tests for config.max_output_tokens in LiteLLMAIHandler.chat_completion: a positive
value is sent as `max_tokens` for every provider, 0 (default) sends nothing, and a
limit set by the extended-thinking path stays authoritative.
"""
import os
from unittest.mock import AsyncMock, MagicMock, patch

import litellm
import openai
import pytest

import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_handler

# Environment variables that LiteLLMAIHandler.__init__ reads or mutates: the AWS
# credential path (entered when AWS_USE_IMDS is set) writes the AWS_* variables,
# and OPENAI_API_KEY influences the litellm.api_key fallback.
_HANDLER_ENV_VARS = (
    "AWS_USE_IMDS",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_REGION_NAME",
    "OPENAI_API_KEY",
)


@pytest.fixture(autouse=True)
def _restore_litellm_globals():
    """LiteLLMAIHandler.__init__ mutates global litellm/openai state and, when
    AWS_USE_IMDS is set, os.environ; snapshot and restore both, and drop
    AWS_USE_IMDS so the AWS credential path never runs in these tests."""
    saved = (litellm.api_key, getattr(litellm, "openai_key", None), openai.api_key)
    saved_env = {name: os.environ.get(name) for name in _HANDLER_ENV_VARS}
    os.environ.pop("AWS_USE_IMDS", None)
    try:
        yield
    finally:
        litellm.api_key = saved[0]
        litellm.openai_key = saved[1]
        openai.api_key = saved[2]
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _make_settings(config_values=None):
    """Minimal settings whose `config.get(key, ...)` serves the given dict."""
    config_values = config_values or {}

    class Config:
        reasoning_effort = None
        ai_timeout = 30
        custom_reasoning_model = False
        max_model_tokens = 32000
        verbosity_level = 0
        seed = -1

        def get(self, key, default=None):
            return config_values.get(key, default)

    return type("Settings", (), {
        "config": Config(),
        "litellm": type("LiteLLM", (), {
            "get": lambda self, key, default=None: default,
        })(),
        "get": lambda self, key, default=None: default,
    })()


def _mock_response():
    mock = MagicMock()
    response = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
    mock.__getitem__.side_effect = response.__getitem__
    mock.dict.return_value = response
    return mock


async def _run(monkeypatch, model, config_values):
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(config_values))
    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
               new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()
        await handler.chat_completion(model=model, system="sys", user="usr")
    return mock_call.call_args[1]


class TestMaxOutputTokens:

    @pytest.mark.asyncio
    async def test_default_sends_no_max_tokens(self, monkeypatch):
        kwargs = await _run(monkeypatch, "bedrock/anthropic.claude-sonnet-5-v1:0", {})
        assert "max_tokens" not in kwargs

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", [
        "bedrock/anthropic.claude-sonnet-5-v1:0",
        "gpt-4o",
    ])
    async def test_positive_value_sent_as_max_tokens(self, monkeypatch, model):
        kwargs = await _run(monkeypatch, model, {"max_output_tokens": 16000})
        assert kwargs["max_tokens"] == 16000

    @pytest.mark.asyncio
    async def test_extended_thinking_limit_stays_authoritative(self, monkeypatch):
        kwargs = await _run(monkeypatch, "claude-3-7-sonnet-20250219", {
            "max_output_tokens": 16000,
            "enable_claude_extended_thinking": True,
            "extended_thinking_budget_tokens": 2048,
            "extended_thinking_max_output_tokens": 4096,
        })
        assert kwargs["max_tokens"] == 4096
        assert kwargs["thinking"] == {"type": "enabled", "budget_tokens": 2048}

    @pytest.mark.asyncio
    async def test_string_override_is_coerced(self, monkeypatch):
        # Dynaconf/env overrides can arrive as strings.
        kwargs = await _run(monkeypatch, "gpt-4o", {"max_output_tokens": "16000"})
        assert kwargs["max_tokens"] == 16000

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["16k", None, 0, -1])
    async def test_unset_or_invalid_values_send_nothing(self, monkeypatch, value):
        kwargs = await _run(monkeypatch, "gpt-4o", {"max_output_tokens": value})
        assert "max_tokens" not in kwargs
