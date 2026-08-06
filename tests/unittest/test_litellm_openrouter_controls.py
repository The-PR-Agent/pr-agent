"""
Tests for the OpenRouter provider-routing / reasoning / output-cap controls in
LiteLLMAIHandler.chat_completion.

The [openrouter] settings (provider_only, provider_order, allow_fallbacks,
reasoning_effort, reasoning_max_tokens, max_tokens) are injected into the request
as `extra_body.provider`, `extra_body.reasoning` and `max_tokens`, but only for
models addressed as "openrouter/...". When nothing is configured the block is a
no-op, and non-openrouter models are never touched.
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


def _make_settings(openrouter=None):
    """Minimal settings whose `.get("openrouter", ...)` returns the given dict."""
    openrouter = openrouter or {}
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
        "get": lambda self, key, default=None: (openrouter if key == "openrouter" else default),
    })()


def _mock_response():
    mock = MagicMock()
    response = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
    mock.__getitem__.side_effect = response.__getitem__
    mock.dict.return_value = response
    return mock


async def _run(monkeypatch, model, openrouter):
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _make_settings(openrouter))
    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
               new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()
        await handler.chat_completion(model=model, system="sys", user="usr")
    return mock_call.call_args[1]


class TestOpenRouterControls:

    @pytest.mark.asyncio
    async def test_provider_only_and_reasoning_effort_and_max_tokens(self, monkeypatch):
        kwargs = await _run(monkeypatch, "openrouter/z-ai/glm-5.2", {
            "provider_only": ["z-ai"],
            "reasoning_effort": "low",
            "max_tokens": 16000,
        })
        assert kwargs["extra_body"] == {"provider": {"only": ["z-ai"]}, "reasoning": {"effort": "low"}}
        assert kwargs["max_tokens"] == 16000

    @pytest.mark.asyncio
    async def test_provider_order_with_allow_fallbacks(self, monkeypatch):
        kwargs = await _run(monkeypatch, "openrouter/z-ai/glm-5.2", {
            "provider_order": ["z-ai", "novita"],
            "allow_fallbacks": False,
        })
        assert kwargs["extra_body"]["provider"] == {"order": ["z-ai", "novita"], "allow_fallbacks": False}

    @pytest.mark.asyncio
    async def test_provider_only_wins_over_order(self, monkeypatch):
        kwargs = await _run(monkeypatch, "openrouter/z-ai/glm-5.2", {
            "provider_only": ["z-ai"],
            "provider_order": ["novita"],
        })
        assert kwargs["extra_body"]["provider"] == {"only": ["z-ai"]}

    @pytest.mark.asyncio
    async def test_reasoning_none_disables(self, monkeypatch):
        kwargs = await _run(monkeypatch, "openrouter/z-ai/glm-5.2", {"reasoning_effort": "none"})
        assert kwargs["extra_body"]["reasoning"] == {"enabled": False}

    @pytest.mark.asyncio
    async def test_reasoning_max_tokens(self, monkeypatch):
        kwargs = await _run(monkeypatch, "openrouter/z-ai/glm-5.2", {
            "reasoning_effort": "high",
            "reasoning_max_tokens": 2048,
        })
        assert kwargs["extra_body"]["reasoning"] == {"effort": "high", "max_tokens": 2048}

    @pytest.mark.asyncio
    async def test_no_config_is_noop(self, monkeypatch):
        kwargs = await _run(monkeypatch, "openrouter/z-ai/glm-5.2", {})
        assert "extra_body" not in kwargs
        assert "max_tokens" not in kwargs

    @pytest.mark.asyncio
    async def test_non_openrouter_model_unaffected(self, monkeypatch):
        kwargs = await _run(monkeypatch, "gpt-4o", {
            "provider_only": ["z-ai"],
            "max_tokens": 16000,
        })
        assert "extra_body" not in kwargs
        assert "max_tokens" not in kwargs

    @pytest.mark.asyncio
    async def test_invalid_reasoning_effort_ignored(self, monkeypatch):
        kwargs = await _run(monkeypatch, "openrouter/z-ai/glm-5.2", {"reasoning_effort": "loww"})
        assert "extra_body" not in kwargs

    @pytest.mark.asyncio
    async def test_reasoning_max_tokens_dropped_when_disabled(self, monkeypatch):
        kwargs = await _run(monkeypatch, "openrouter/z-ai/glm-5.2", {
            "reasoning_effort": "none",
            "reasoning_max_tokens": 2048,
        })
        assert kwargs["extra_body"]["reasoning"] == {"enabled": False}

    @pytest.mark.asyncio
    async def test_string_overrides_are_coerced(self, monkeypatch):
        # Dynaconf/env overrides can arrive as strings; they must not crash or
        # be split into characters.
        kwargs = await _run(monkeypatch, "openrouter/z-ai/glm-5.2", {
            "provider_only": "z-ai",
            "max_tokens": "16000",
        })
        assert kwargs["extra_body"]["provider"] == {"only": ["z-ai"]}
        assert kwargs["max_tokens"] == 16000

    @pytest.mark.asyncio
    async def test_allow_fallbacks_string_false(self, monkeypatch):
        kwargs = await _run(monkeypatch, "openrouter/z-ai/glm-5.2", {
            "provider_order": ["z-ai", "novita"],
            "allow_fallbacks": "false",
        })
        assert kwargs["extra_body"]["provider"]["allow_fallbacks"] is False

    @pytest.mark.asyncio
    async def test_non_numeric_max_tokens_ignored(self, monkeypatch):
        kwargs = await _run(monkeypatch, "openrouter/z-ai/glm-5.2", {"max_tokens": "16k"})
        assert "max_tokens" not in kwargs

    @pytest.mark.asyncio
    async def test_azure_mode_does_not_mask_openrouter(self, monkeypatch):
        # Azure mode must not rewrite "openrouter/..." to "azure/openrouter/...":
        # that would misroute the request and skip the OpenRouter controls block.
        monkeypatch.setattr(litellm_handler, "get_settings",
                            lambda: _make_settings({"provider_only": ["z-ai"]}))
        with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
                   new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _mock_response()
            handler = litellm_handler.LiteLLMAIHandler()
            handler.azure = True
            await handler.chat_completion(model="openrouter/z-ai/glm-5.2", system="sys", user="usr")
        kwargs = mock_call.call_args[1]
        assert kwargs["model"] == "openrouter/z-ai/glm-5.2"
        assert kwargs["extra_body"]["provider"] == {"only": ["z-ai"]}
