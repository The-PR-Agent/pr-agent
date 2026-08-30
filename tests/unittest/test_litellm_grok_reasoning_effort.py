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
        litellm=SimpleNamespace(get=lambda key, default=None: default, custom_llm_provider=""),
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
@pytest.mark.parametrize("model", ["grok-4.20-multi-agent", "xai/grok-4.20-multi-agent"])
async def test_enabled_grok_reasoning_effort_reaches_provider_prefixed_models(monkeypatch, model):
    kwargs = await _run_completion(monkeypatch, model, reasoning_effort="high", enabled=True)

    assert kwargs["reasoning_effort"] == "high"
    assert kwargs["temperature"] == 0.2


@pytest.mark.asyncio
async def test_allowlist_is_only_added_when_litellm_does_not_report_the_param(monkeypatch):
    """The allowlist pushes reasoning_effort past litellm's capability check, so it is only added
    for model IDs litellm does not already mark as reasoning-capable.

    Reported by @IsmaelMartinez on #2530: applying it unconditionally also forced the field onto
    grok-2/grok-3, which litellm deliberately drops.
    """
    monkeypatch.setattr(
        litellm_handler.litellm, "get_supported_openai_params", lambda **kw: []
    )
    kwargs = await _run_completion(
        monkeypatch, "xai/grok-4.20-multi-agent", reasoning_effort="high", enabled=True
    )

    assert kwargs["allowed_openai_params"] == ["reasoning_effort"]

    monkeypatch.setattr(
        litellm_handler.litellm, "get_supported_openai_params", lambda **kw: ["reasoning_effort"]
    )
    kwargs = await _run_completion(
        monkeypatch, "xai/grok-4.20-multi-agent", reasoning_effort="high", enabled=True
    )

    assert "allowed_openai_params" not in kwargs


@pytest.mark.asyncio
async def test_grok_reasoning_effort_is_opt_in(monkeypatch):
    kwargs = await _run_completion(
        monkeypatch, "xai/grok-4.20-multi-agent", reasoning_effort="high", enabled=False
    )

    assert "reasoning_effort" not in kwargs
    assert "allowed_openai_params" not in kwargs
    assert kwargs["temperature"] == 0.2


@pytest.mark.asyncio
async def test_invalid_grok_reasoning_effort_falls_back_to_medium(monkeypatch):
    kwargs = await _run_completion(
        monkeypatch, "xai/grok-4.20-multi-agent", reasoning_effort="invalid", enabled=True
    )

    assert kwargs["reasoning_effort"] == "medium"


@pytest.mark.asyncio
async def test_natively_supported_grok_models_are_left_to_main_handling(monkeypatch):
    """Grok families that main already covers via GROK_REASONING_EFFORT_LEVELS (#2871) must not be
    re-handled by this opt-in branch: main clamps xhigh -> high for grok-4.5 on its own, and the
    flag must not override that."""
    kwargs = await _run_completion(
        monkeypatch,
        "xai/grok-4.5",
        reasoning_effort="xhigh",
        enabled=True,
    )

    assert kwargs["reasoning_effort"] == "high"
    assert "allowed_openai_params" not in kwargs


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


@pytest.mark.asyncio
async def test_openrouter_grok_is_left_to_the_openrouter_block(monkeypatch):
    """OpenRouter-routed Grok models must not pick up top-level reasoning_effort from [config].

    Reported by @IsmaelMartinez on #2530: `grok_model` is the basename, so
    `openrouter/x-ai/grok-4` also matched and got `reasoning_effort` from `[config]` while the
    OpenRouter block below was already setting `extra_body.reasoning` from `[openrouter]`.
    """
    kwargs = await _run_completion(
        monkeypatch,
        "openrouter/x-ai/grok-4.5",
        reasoning_effort="high",
        enabled=True,
    )

    assert "allowed_openai_params" not in kwargs
