from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_handler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler


def _settings(reasoning_effort="medium", enabled=False):
    flags = {"enable_claude_adaptive_thinking": enabled}
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


async def _run_completion(monkeypatch, model, reasoning_effort="medium", enabled=False):
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _settings(reasoning_effort, enabled))
    with patch(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
        new_callable=AsyncMock,
    ) as completion:
        completion.return_value = _response()
        handler = LiteLLMAIHandler()
        await handler.chat_completion(model=model, system="sys", user="usr")
        return completion.call_args.kwargs


@pytest.mark.parametrize(
    "model, expected",
    [
        ("anthropic/claude-opus-4-8", True),
        ("bedrock/us.anthropic.claude-opus-4-7-v1:0", True),
        ("vertex_ai/claude-sonnet-5", True),
        ("anthropic/claude-fable-5", True),
        ("anthropic/claude-opus-4-6", False),
        ("anthropic/claude-sonnet-50", False),
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
    assert kwargs["temperature"] == 1
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
