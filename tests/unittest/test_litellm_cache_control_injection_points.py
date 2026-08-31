"""LITELLM.CACHE_CONTROL_INJECTION_POINTS pass-through for Anthropic prompt caching (PR #2405).

Covers the config-boundary handling that Qodo flagged: native TOML arrays, JSON-string
fallback, and deterministic config errors that must surface (not be retried or wrapped).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import openai
import pytest
import tenacity

import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_handler


class FakeBox:
    def __init__(self, values=None, **attrs):
        self._values = values or {}
        for key, value in attrs.items():
            setattr(self, key, value)

    def get(self, key, default=None):
        return self._values.get(key, default)


class FakeSettings:
    def __init__(self, config_values=None, settings_values=None):
        self.config = FakeBox(
            config_values or {},
            reasoning_effort=None,
            ai_timeout=30,
            custom_reasoning_model=False,
            max_model_tokens=32000,
            verbosity_level=0,
            model="gpt-4o",
        )
        self.litellm = FakeBox()
        self._settings_values = settings_values or {}

    def get(self, key, default=None):
        return self._settings_values.get(key, default)


def _mock_response():
    mock = MagicMock()
    response = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
    mock.__getitem__.side_effect = response.__getitem__
    mock.dict.return_value = response
    return mock


def _settings(value):
    return lambda: FakeSettings(settings_values={"LITELLM.CACHE_CONTROL_INJECTION_POINTS": value})


@pytest.mark.asyncio
async def test_native_toml_list_is_passed_through(monkeypatch):
    # TOML arrays are parsed by Dynaconf into native Python lists; accept them as-is.
    points = [{"location": "message", "role": "system"}]
    monkeypatch.setattr(litellm_handler, "get_settings", _settings(points))

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()
        await handler.chat_completion(model="claude-sonnet-5", system="sys", user="usr")

    assert mock_call.call_args.kwargs["cache_control_injection_points"] == points


@pytest.mark.asyncio
async def test_json_string_is_parsed(monkeypatch):
    # A JSON string (e.g. from an environment-variable override) is decoded to a list.
    monkeypatch.setattr(litellm_handler, "get_settings", _settings('[{"location": "message", "role": "system"}]'))

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()
        await handler.chat_completion(model="claude-sonnet-5", system="sys", user="usr")

    assert mock_call.call_args.kwargs["cache_control_injection_points"] == [{"location": "message", "role": "system"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, [], ""])
async def test_absent_or_empty_setting_is_backwards_compatible(monkeypatch, value):
    monkeypatch.setattr(litellm_handler, "get_settings", _settings(value))

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()
        await handler.chat_completion(model="claude-sonnet-5", system="sys", user="usr")

    assert "cache_control_injection_points" not in mock_call.call_args.kwargs


@pytest.mark.asyncio
async def test_invalid_json_string_raises_value_error_and_is_not_retried(monkeypatch):
    # A malformed config is deterministic: it must surface as ValueError, never be wrapped as
    # openai.APIError (which the @retry decorator would retry) and never reach the model call.
    monkeypatch.setattr(litellm_handler, "get_settings", _settings("{not valid json"))

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()

        with pytest.raises(ValueError) as exc_info:
            await handler.chat_completion(model="claude-sonnet-5", system="sys", user="usr")

    assert not isinstance(exc_info.value, (openai.APIError, tenacity.RetryError))
    assert "CACHE_CONTROL_INJECTION_POINTS" in str(exc_info.value)
    assert mock_call.call_count == 0


@pytest.mark.asyncio
# Includes falsy-but-malformed values (0, False, {}) that must NOT be silently treated as "unset".
@pytest.mark.parametrize("value", [{"location": "message"}, 42, {}, 0, False])
async def test_non_list_value_raises_value_error(monkeypatch, value):
    monkeypatch.setattr(litellm_handler, "get_settings", _settings(value))

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()

        with pytest.raises(ValueError) as exc_info:
            await handler.chat_completion(model="claude-sonnet-5", system="sys", user="usr")

    assert "must be a JSON/TOML array" in str(exc_info.value)
    assert mock_call.call_count == 0


@pytest.mark.asyncio
async def test_not_injected_for_non_anthropic_model(monkeypatch):
    # The kwarg is Anthropic-specific; a valid config must not be passed through for other providers,
    # so litellm.drop_params=off deployments don't get their request rejected.
    points = [{"location": "message", "role": "system"}]
    monkeypatch.setattr(litellm_handler, "get_settings", _settings(points))

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()
        await handler.chat_completion(model="gpt-4o", system="sys", user="usr")

    assert "cache_control_injection_points" not in mock_call.call_args.kwargs
