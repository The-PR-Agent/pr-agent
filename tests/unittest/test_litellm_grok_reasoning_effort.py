from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_handler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler


def _settings(reasoning_effort="medium", enabled=False):
    flags = {"enable_grok_reasoning_effort": enabled}
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


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["grok-4.5", "xai/grok-4.5"])
async def test_enabled_grok_reasoning_effort_reaches_provider_prefixed_models(monkeypatch, model):
    kwargs = await _run_completion(monkeypatch, model, reasoning_effort="high", enabled=True)

    assert kwargs["reasoning_effort"] == "high"
    assert kwargs["allowed_openai_params"] == ["reasoning_effort"]
    assert kwargs["temperature"] == 0.2


@pytest.mark.asyncio
async def test_grok_reasoning_effort_is_opt_in(monkeypatch):
    kwargs = await _run_completion(monkeypatch, "xai/grok-4.5", reasoning_effort="high", enabled=False)

    assert "reasoning_effort" not in kwargs
    assert "allowed_openai_params" not in kwargs
    assert kwargs["temperature"] == 0.2


@pytest.mark.asyncio
async def test_invalid_grok_reasoning_effort_falls_back_to_medium(monkeypatch):
    kwargs = await _run_completion(monkeypatch, "xai/grok-4.5", reasoning_effort="invalid", enabled=True)

    assert kwargs["reasoning_effort"] == "medium"


@pytest.mark.asyncio
async def test_grok_4_5_rejects_non_xai_effort_values(monkeypatch):
    kwargs = await _run_completion(
        monkeypatch,
        "xai/grok-4.5",
        reasoning_effort="xhigh",
        enabled=True,
    )

    assert kwargs["reasoning_effort"] == "medium"


@pytest.mark.asyncio
async def test_grok_multi_agent_accepts_xhigh(monkeypatch):
    kwargs = await _run_completion(
        monkeypatch,
        "xai/grok-4.20-multi-agent",
        reasoning_effort="xhigh",
        enabled=True,
    )

    assert kwargs["reasoning_effort"] == "xhigh"


@pytest.mark.asyncio
async def test_grok_reasoning_effort_does_not_overmatch_model_basename(monkeypatch):
    kwargs = await _run_completion(monkeypatch, "xai/my-grok-4.5", reasoning_effort="high", enabled=True)

    assert "reasoning_effort" not in kwargs
    assert kwargs["temperature"] == 0.2
