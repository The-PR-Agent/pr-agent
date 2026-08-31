import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import litellm
import openai
import pytest

import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_handler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler

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


def _settings(reasoning_effort="medium", enabled=False, extended_enabled=False):
    flags = {
        "enable_claude_adaptive_thinking": enabled,
        "enable_claude_extended_thinking": extended_enabled,
    }
    config = SimpleNamespace(
        reasoning_effort=reasoning_effort,
        ai_timeout=120,
        custom_reasoning_model=False,
        max_model_tokens=32000,
        verbosity_level=0,
        get=lambda key, default=None: flags.get(key, default),
    )
    return SimpleNamespace(
        config=config,
        litellm=SimpleNamespace(get=lambda key, default=None: default),
        get=lambda key, default=None: default,
    )


def _response():
    response = MagicMock()
    payload = {"choices": [{"message": {"content": "test"}, "finish_reason": "stop"}]}
    response.__getitem__.side_effect = payload.__getitem__
    response.dict.return_value = payload
    return response


async def _run_completion(monkeypatch, model, reasoning_effort="medium", enabled=False,
                          extended_enabled=False, extended_override=None):
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: _settings(reasoning_effort, enabled, extended_enabled),
    )
    with patch(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
        new_callable=AsyncMock,
    ) as completion:
        completion.return_value = _response()
        handler = LiteLLMAIHandler()
        if extended_override is not None:
            handler.claude_extended_thinking_models = extended_override
        await handler.chat_completion(model=model, system="sys", user="usr")
        return completion.call_args.kwargs


@pytest.mark.parametrize(
    "model, expected",
    [
        ("anthropic/claude-opus-4-8", True),
        ("bedrock/us.anthropic.claude-opus-4-7-v1:0", True),
        ("vertex_ai/claude-sonnet-5", True),
        ("anthropic/claude-opus-5", True),
        ("bedrock/us.anthropic.claude-opus-5", True),
        ("anthropic/claude-fable-5", True),
        ("anthropic/claude-opus-4-6", False),
        ("anthropic/claude-sonnet-50", False),
        ("anthropic/claude-opus-50", False),
        ("anthropic/my-opus-4-8", False),
    ],
)
def test_detects_adaptive_thinking_models_across_providers(model, expected):
    assert LiteLLMAIHandler._is_claude_adaptive_thinking_model(model) is expected


@pytest.mark.asyncio
async def test_enabled_adaptive_thinking_sends_anthropic_payload(monkeypatch):
    kwargs = await _run_completion(
        monkeypatch,
        "anthropic/claude-opus-4-8",
        reasoning_effort="high",
        enabled=True,
    )

    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"] == {"effort": "high"}
    assert "temperature" not in kwargs
    assert "reasoning_effort" not in kwargs


@pytest.mark.asyncio
async def test_adaptive_thinking_accepts_max_effort_without_enum_dependency(monkeypatch):
    kwargs = await _run_completion(
        monkeypatch,
        "anthropic/claude-sonnet-5",
        reasoning_effort="max",
        enabled=True,
    )

    assert kwargs["output_config"] == {"effort": "max"}


@pytest.mark.asyncio
async def test_adaptive_thinking_omits_unsupported_effort(monkeypatch):
    kwargs = await _run_completion(
        monkeypatch,
        "anthropic/claude-opus-4-8",
        reasoning_effort="minimal",
        enabled=True,
    )

    assert kwargs["thinking"] == {"type": "adaptive"}
    assert "output_config" not in kwargs


@pytest.mark.asyncio
async def test_adaptive_thinking_is_opt_in_and_does_not_touch_older_models(monkeypatch):
    disabled = await _run_completion(
        monkeypatch,
        "anthropic/claude-opus-4-8",
        reasoning_effort="high",
        enabled=False,
    )
    older_model = await _run_completion(
        monkeypatch,
        "anthropic/claude-opus-4-6",
        reasoning_effort="high",
        enabled=True,
    )

    assert "thinking" not in disabled
    assert "output_config" not in disabled
    assert "thinking" not in older_model
    assert "output_config" not in older_model


@pytest.mark.asyncio
async def test_adaptive_only_model_in_extended_override_does_not_get_budget_tokens(monkeypatch):
    """An adaptive-only model wrongly placed in claude_extended_thinking_models_override must not
    receive the legacy budget_tokens payload, even with adaptive thinking left disabled.

    Reported by @IsmaelMartinez on #2531: the TOML comment warned about this configuration but the
    code did not enforce it, so the request was still shaped in a way the provider rejects (400).
    """
    kwargs = await _run_completion(
        monkeypatch,
        "anthropic/claude-opus-5",
        reasoning_effort="high",
        enabled=False,
        extended_enabled=True,
        extended_override=["anthropic/claude-opus-5"],
    )

    assert "thinking" not in kwargs
    assert "output_config" not in kwargs


@pytest.mark.asyncio
async def test_non_adaptive_model_in_extended_override_still_gets_extended_thinking(monkeypatch):
    """The adaptive-only guard must not disturb ordinary extended-thinking models."""
    kwargs = await _run_completion(
        monkeypatch,
        "anthropic/claude-opus-4-6",
        reasoning_effort="high",
        enabled=False,
        extended_enabled=True,
        extended_override=["anthropic/claude-opus-4-6"],
    )

    assert kwargs["thinking"]["type"] == "enabled"
    assert "budget_tokens" in kwargs["thinking"]
