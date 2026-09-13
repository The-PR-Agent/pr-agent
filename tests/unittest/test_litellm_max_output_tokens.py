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

# Clear these credential environment variables before constructing handlers
# to keep the tests independent of deployment-specific AWS or OpenAI credentials.
_HANDLER_ENV_VARS = (
    *litellm_handler.AWS_CREDENTIAL_CHAIN_ENV_VARS,
    "AWS_USE_IMDS",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_REGION_NAME",
    "AWS_BEARER_TOKEN_BEDROCK",
    "OPENAI_API_KEY",
)


@pytest.fixture(autouse=True)
def _restore_litellm_globals():
    """Clear credential environment and the shared LiteLLM key; restore saved state after each test."""
    saved = (litellm.api_key, getattr(litellm, "openai_key", None), openai.api_key)
    saved_env = {name: os.environ.get(name) for name in _HANDLER_ENV_VARS}
    for name in _HANDLER_ENV_VARS:
        os.environ.pop(name, None)
    litellm.api_key = None
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


def test_fixture_clears_and_restores_ambient_litellm_key(monkeypatch):
    monkeypatch.setattr(litellm, "api_key", "test-ambient-key")
    fixture = _restore_litellm_globals.__wrapped__()
    next(fixture)
    try:
        assert litellm.api_key is None
    finally:
        with pytest.raises(StopIteration):
            next(fixture)
    assert litellm.api_key == "test-ambient-key"


def _make_settings(config_values=None, openrouter=None, custom_llm_provider=""):
    """Minimal settings whose `config.get(key, ...)` serves the given dict."""
    config_values = config_values or {}
    settings_values = {
        "aws.AWS_ACCESS_KEY_ID": "test-access-key",
        "aws.AWS_SECRET_ACCESS_KEY": "test-secret-key",
        "aws.AWS_REGION_NAME": "us-east-1",
    }

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
            "custom_llm_provider": custom_llm_provider,
            "get": lambda self, key, default=None: default,
        })(),
        "get": lambda self, key, default=None: (
            (openrouter or {}) if key == "openrouter" else settings_values.get(key, default)
        ),
    })()


def _mock_response():
    mock = MagicMock()
    response = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
    mock.__getitem__.side_effect = response.__getitem__
    mock.dict.return_value = response
    return mock


async def _run(
    monkeypatch,
    model,
    config_values,
    openrouter=None,
    custom_llm_provider="",
    reserve_default=None,
):
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _make_settings(config_values, openrouter, custom_llm_provider),
    )
    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
               new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()
        exposed_limit = handler.get_output_token_limit(model)
        exposed_reserve = (
            handler.get_output_token_reserve(model, reserve_default)
            if reserve_default is not None
            else None
        )
        await handler.chat_completion(model=model, system="sys", user="usr")
    result = (mock_call.call_args[1], exposed_limit)
    return (*result, exposed_reserve) if reserve_default is not None else result


class TestMaxOutputTokens:

    @pytest.mark.asyncio
    async def test_default_sends_no_max_tokens(self, monkeypatch):
        kwargs, exposed_limit = await _run(monkeypatch, "bedrock/anthropic.claude-sonnet-5-v1:0", {})
        assert "max_tokens" not in kwargs
        assert exposed_limit == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", [
        "bedrock/anthropic.claude-sonnet-5-v1:0",
        "gpt-4o",
    ])
    async def test_positive_value_sent_as_max_tokens(self, monkeypatch, model):
        kwargs, exposed_limit = await _run(monkeypatch, model, {"max_output_tokens": 16000})
        assert kwargs["max_tokens"] == 16000
        assert exposed_limit == 16000

    @pytest.mark.asyncio
    async def test_extended_thinking_limit_stays_authoritative(self, monkeypatch):
        kwargs, exposed_limit = await _run(monkeypatch, "claude-3-7-sonnet-20250219", {
            "max_output_tokens": 16000,
            "enable_claude_extended_thinking": True,
            "extended_thinking_budget_tokens": 2048,
            "extended_thinking_max_output_tokens": 4096,
        })
        assert kwargs["max_tokens"] == 4096
        assert exposed_limit == 4096
        assert kwargs["thinking"] == {"type": "enabled", "budget_tokens": 2048}

    @pytest.mark.asyncio
    async def test_string_override_is_coerced(self, monkeypatch):
        # Dynaconf/env overrides can arrive as strings.
        kwargs, exposed_limit = await _run(monkeypatch, "gpt-4o", {"max_output_tokens": "16000"})
        assert kwargs["max_tokens"] == 16000
        assert exposed_limit == 16000

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["16k", None, 0, -1, float("nan"), float("inf"), "1" * 5_000])
    async def test_unset_or_invalid_values_send_nothing(self, monkeypatch, value):
        kwargs, exposed_limit = await _run(monkeypatch, "gpt-4o", {"max_output_tokens": value})
        assert "max_tokens" not in kwargs
        assert exposed_limit == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("value", "expected"), [(400.5, 400), (True, 1), (False, 0)])
    async def test_existing_int_coercion_is_preserved(self, monkeypatch, value, expected):
        kwargs, exposed_limit = await _run(monkeypatch, "gpt-4o", {"max_output_tokens": value})

        assert exposed_limit == expected
        if expected:
            assert kwargs["max_tokens"] == expected
        else:
            assert "max_tokens" not in kwargs

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("model", "custom_llm_provider"),
        [
            ("openrouter/google/gemini-2.5-pro", ""),
            ("google/gemini-2.5-pro", "openrouter"),
        ],
    )
    async def test_openrouter_only_limit_is_exposed_and_sent(
        self, monkeypatch, model, custom_llm_provider
    ):
        kwargs, exposed_limit = await _run(
            monkeypatch,
            model,
            {},
            openrouter={"max_tokens": 16000},
            custom_llm_provider=custom_llm_provider,
        )

        assert exposed_limit == 16000
        assert kwargs["max_tokens"] == 16000

    @pytest.mark.asyncio
    async def test_openrouter_limit_caps_general_limit(self, monkeypatch):
        kwargs, exposed_limit = await _run(
            monkeypatch,
            "openrouter/google/gemini-2.5-pro",
            {"max_output_tokens": 16000},
            openrouter={"max_tokens": 4096},
        )

        assert exposed_limit == 4096
        assert kwargs["max_tokens"] == 4096

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("config_values", "openrouter", "expected_limit", "expected_reserve"),
        [
            ({}, {"reasoning_max_tokens": 4096}, 0, 6096),
            ({"max_output_tokens": 8000}, {"reasoning_max_tokens": 4096}, 8000, 8000),
            ({}, {"reasoning_max_tokens": 4096, "reasoning_effort": "none"}, 0, 2000),
        ],
    )
    async def test_openrouter_reasoning_expands_only_the_default_reserve(
        self,
        monkeypatch,
        config_values,
        openrouter,
        expected_limit,
        expected_reserve,
    ):
        kwargs, exposed_limit, exposed_reserve = await _run(
            monkeypatch,
            "openrouter/google/gemini-2.5-pro",
            config_values,
            openrouter=openrouter,
            reserve_default=2000,
        )

        assert exposed_limit == expected_limit
        assert exposed_reserve == expected_reserve
        if openrouter.get("reasoning_effort") == "none":
            assert kwargs["extra_body"]["reasoning"] == {"enabled": False}
        else:
            assert kwargs["extra_body"]["reasoning"] == {"max_tokens": 4096}

    @pytest.mark.asyncio
    async def test_openrouter_grok_clamped_none_still_reserves_reasoning(self, monkeypatch):
        kwargs, exposed_limit, exposed_reserve = await _run(
            monkeypatch,
            "openrouter/x-ai/grok-4.6",
            {},
            openrouter={"reasoning_max_tokens": 8000, "reasoning_effort": "none"},
            reserve_default=2000,
        )

        assert exposed_limit == 0
        assert exposed_reserve == 10000
        assert kwargs["extra_body"]["reasoning"] == {"max_tokens": 8000}

    @pytest.mark.asyncio
    async def test_openrouter_astra_uses_one_capped_output_limit(self, monkeypatch):
        kwargs, exposed_limit = await _run(
            monkeypatch,
            "gpt-6-astra",
            {"max_output_tokens": 16000},
            openrouter={"max_tokens": 4096},
            custom_llm_provider="openrouter",
        )

        assert exposed_limit == 4096
        assert kwargs["max_completion_tokens"] == 4096
        assert "max_tokens" not in kwargs

    @pytest.mark.asyncio
    async def test_openrouter_limit_caps_extended_thinking_limit(self, monkeypatch):
        model = "openrouter/anthropic/claude-3-7-sonnet-20250219"
        kwargs, exposed_limit = await _run(
            monkeypatch,
            model,
            {
                "max_output_tokens": 16000,
                "enable_claude_extended_thinking": True,
                "extended_thinking_budget_tokens": 1024,
                "extended_thinking_max_output_tokens": 4096,
                "claude_extended_thinking_models_override": [model],
            },
            openrouter={"max_tokens": 2048},
        )

        assert exposed_limit == 2048
        assert kwargs["thinking"] == {"type": "enabled", "budget_tokens": 1024}
        assert kwargs["max_tokens"] == 2048

    @pytest.mark.asyncio
    @pytest.mark.parametrize("adaptive_enabled", [True, False])
    async def test_adaptive_only_model_never_uses_legacy_extended_limit(
        self, monkeypatch, adaptive_enabled
    ):
        model = "anthropic/claude-opus-5"
        kwargs, exposed_limit = await _run(monkeypatch, model, {
            "max_output_tokens": 16000,
            "enable_claude_adaptive_thinking": adaptive_enabled,
            "enable_claude_extended_thinking": True,
            "extended_thinking_budget_tokens": 2048,
            "extended_thinking_max_output_tokens": 4096,
            "claude_extended_thinking_models_override": [model],
        })

        assert exposed_limit == 16000
        assert kwargs["max_tokens"] == 16000
        if adaptive_enabled:
            assert kwargs["thinking"] == {"type": "adaptive"}
        else:
            assert "thinking" not in kwargs

    @pytest.mark.asyncio
    async def test_general_limit_is_live_while_handler_controls_are_snapshots(self, monkeypatch):
        model = "openrouter/google/gemini-2.5-pro"
        config_values = {"max_output_tokens": 16000}
        openrouter = {"max_tokens": 4096}
        active_settings = _make_settings(config_values, openrouter)
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: active_settings)
        handler = litellm_handler.LiteLLMAIHandler()

        config_values["max_output_tokens"] = 2048
        openrouter["max_tokens"] = 1024

        with patch(
            "pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
            new_callable=AsyncMock,
        ) as mock_call:
            mock_call.return_value = _mock_response()
            assert handler.get_output_token_limit(model) == 2048
            await handler.chat_completion(model=model, system="sys", user="usr")

        assert mock_call.call_args.kwargs["max_tokens"] == 2048

    @pytest.mark.asyncio
    async def test_extended_thinking_limit_is_snapshotted_for_accessor_and_request(self, monkeypatch):
        model = "claude-3-7-sonnet-20250219"
        config_values = {
            "enable_claude_extended_thinking": True,
            "extended_thinking_budget_tokens": 1024,
            "extended_thinking_max_output_tokens": 4096,
        }
        active_settings = _make_settings(config_values)
        monkeypatch.setattr(litellm_handler, "get_settings", lambda: active_settings)
        handler = litellm_handler.LiteLLMAIHandler()

        config_values["extended_thinking_budget_tokens"] = 2048
        config_values["extended_thinking_max_output_tokens"] = 8192

        with patch(
            "pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
            new_callable=AsyncMock,
        ) as mock_call:
            mock_call.return_value = _mock_response()
            assert handler.get_output_token_limit(model) == 4096
            await handler.chat_completion(model=model, system="sys", user="usr")

        assert mock_call.call_args.kwargs["thinking"] == {"type": "enabled", "budget_tokens": 1024}
        assert mock_call.call_args.kwargs["max_tokens"] == 4096
