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


def _warn_settings(points=None):
    return lambda: FakeSettings(
        settings_values={"LITELLM.CACHE_CONTROL_INJECTION_POINTS": points or [{"location": "message", "role": "system"}]}
    )


@pytest.mark.asyncio
async def test_warns_once_when_model_does_not_support_prompt_caching(monkeypatch):
    # The call must still go through (caching is best effort), but the operator needs one
    # audible warning per (model, reason) that their config cannot take effect.
    mock_logger = MagicMock()
    monkeypatch.setattr(litellm_handler, "get_logger", lambda: mock_logger)
    monkeypatch.setattr(litellm_handler, "_ANTHROPIC_CACHE_WARNING_LOG", set())
    monkeypatch.setattr(litellm_handler, "get_settings", _warn_settings())
    monkeypatch.setattr(litellm_handler.litellm.utils, "supports_prompt_caching", lambda model: False)

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()
        await handler.chat_completion(model="claude-sonnet-5", system="sys", user="usr")
        await handler.chat_completion(model="claude-sonnet-5", system="sys", user="usr")

    assert mock_call.call_count == 2
    warning_texts = [call.args[0] for call in mock_logger.warning.call_args_list]
    assert len([text for text in warning_texts if "does not support prompt caching" in text]) == 1
    assert "claude-sonnet-5" in warning_texts[0]


@pytest.mark.asyncio
async def test_warns_once_for_non_anthropic_model_even_though_forwarding_is_gated(monkeypatch):
    # A non-Claude model never gets the kwarg forwarded, but it must no longer be silently
    # dropped in a debug line: the operator gets one warning naming the model and the reason.
    mock_logger = MagicMock()
    monkeypatch.setattr(litellm_handler, "get_logger", lambda: mock_logger)
    monkeypatch.setattr(litellm_handler, "_ANTHROPIC_CACHE_WARNING_LOG", set())
    monkeypatch.setattr(litellm_handler, "get_settings", _warn_settings())

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()
        await handler.chat_completion(model="gpt-4o", system="sys", user="usr")
        await handler.chat_completion(model="gpt-4o", system="sys", user="usr")

    assert mock_call.call_count == 2
    assert "cache_control_injection_points" not in mock_call.call_args.kwargs
    warning_texts = [call.args[0] for call in mock_logger.warning.call_args_list]
    assert len([text for text in warning_texts if "does not route to an Anthropic Claude model" in text]) == 1


def test_anthropic_routed_alias_without_metadata_is_silent(monkeypatch):
    # A provider-aliased Claude deployment (e.g. anthropic/my-deployment) may be absent from
    # litellm's cost map: best-effort metadata lookups must not emit a false warning.
    mock_logger = MagicMock()
    monkeypatch.setattr(litellm_handler, "get_logger", lambda: mock_logger)
    monkeypatch.setattr(litellm_handler, "_ANTHROPIC_CACHE_WARNING_LOG", set())
    monkeypatch.setattr(litellm_handler.litellm.utils, "supports_prompt_caching", MagicMock(side_effect=RuntimeError("no model")))

    handler = litellm_handler.LiteLLMAIHandler()
    handler._warn_prompt_cache_conditions(
        "my-claude-gateway", "sys", "usr", [{"location": "message", "role": "system"}], request_provider="anthropic"
    )

    assert mock_logger.warning.call_count == 0


def test_openrouter_claude_route_warns_non_anthropic(monkeypatch):
    # An openrouter/.../claude-... route cannot attach the Anthropic-only cache_control
    # kwarg either, but its name used to bypass the route warning: a resolved non-Anthropic
    # provider is authoritative over the model-name heuristic.
    mock_logger = MagicMock()
    monkeypatch.setattr(litellm_handler, "get_logger", lambda: mock_logger)
    monkeypatch.setattr(litellm_handler, "_ANTHROPIC_CACHE_WARNING_LOG", set())

    handler = litellm_handler.LiteLLMAIHandler()
    handler._warn_prompt_cache_conditions(
        "openrouter/anthropic/claude-3.5-sonnet",
        "sys",
        "usr",
        [{"location": "message", "role": "system"}],
        request_provider="openrouter",
    )

    assert mock_logger.warning.call_count == 1
    warning_texts = [call.args[0] for call in mock_logger.warning.call_args_list]
    assert len([text for text in warning_texts if "does not route to an Anthropic Claude model" in text]) == 1


@pytest.mark.asyncio
async def test_warns_when_cached_prefix_below_model_minimum(monkeypatch):
    mock_logger = MagicMock()
    monkeypatch.setattr(litellm_handler, "get_logger", lambda: mock_logger)
    monkeypatch.setattr(litellm_handler, "_ANTHROPIC_CACHE_WARNING_LOG", set())
    monkeypatch.setattr(litellm_handler, "get_settings", _warn_settings())
    monkeypatch.setattr(litellm_handler.litellm.utils, "supports_prompt_caching", lambda model: True)
    monkeypatch.setattr(litellm_handler.litellm, "get_model_info", lambda model: {"prompt_cache_min_tokens": 4096})
    monkeypatch.setattr(
        litellm_handler.LiteLLMAIHandler,
        "_estimate_cached_prefix_tokens",
        staticmethod(lambda system, user, points: 120),
    )

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()
        await handler.chat_completion(model="claude-sonnet-5", system="sys", user="usr")

    assert mock_call.call_count == 1
    warning_texts = [call.args[0] for call in mock_logger.warning.call_args_list]
    assert len([text for text in warning_texts if "below the model's 4096 token minimum" in text]) == 1


@pytest.mark.asyncio
async def test_no_warning_when_support_and_prefix_match(monkeypatch):
    mock_logger = MagicMock()
    monkeypatch.setattr(litellm_handler, "get_logger", lambda: mock_logger)
    monkeypatch.setattr(litellm_handler, "_ANTHROPIC_CACHE_WARNING_LOG", set())
    monkeypatch.setattr(litellm_handler, "get_settings", _warn_settings())
    monkeypatch.setattr(litellm_handler.litellm.utils, "supports_prompt_caching", lambda model: True)
    monkeypatch.setattr(litellm_handler.litellm, "get_model_info", lambda model: {"prompt_cache_min_tokens": 1024})
    monkeypatch.setattr(
        litellm_handler.LiteLLMAIHandler,
        "_estimate_cached_prefix_tokens",
        staticmethod(lambda system, user, points: 2000),
    )

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()
        await handler.chat_completion(model="claude-sonnet-5", system="sys", user="usr")

    assert mock_logger.warning.call_count == 0


@pytest.mark.asyncio
async def test_no_warning_without_cache_metadata_but_call_proceeds(monkeypatch):
    # A metadata lookup failure must be silent, not an error: the warning is best effort.
    mock_logger = MagicMock()
    monkeypatch.setattr(litellm_handler, "get_logger", lambda: mock_logger)
    monkeypatch.setattr(litellm_handler, "_ANTHROPIC_CACHE_WARNING_LOG", set())
    monkeypatch.setattr(litellm_handler, "get_settings", _warn_settings())
    monkeypatch.setattr(litellm_handler.litellm.utils, "supports_prompt_caching", MagicMock(side_effect=RuntimeError("no model")))

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()
        await handler.chat_completion(model="claude-sonnet-5", system="sys", user="usr")

    assert mock_call.call_count == 1
    assert mock_logger.warning.call_count == 0


def test_estimate_counts_prompt_prefix_up_to_targeted_message(monkeypatch):
    class _FakeEncoder:
        @staticmethod
        def encode(text, **kwargs):
            return [1] * ((len(text) if text else 0) // 2 + 1)

    monkeypatch.setattr(
        "pr_agent.algo.token_handler.TokenEncoder.get_token_encoder",
        staticmethod(lambda model: _FakeEncoder()),
    )

    handler = litellm_handler.LiteLLMAIHandler()
    system_only = handler._estimate_cached_prefix_tokens("pineapple", "banana", [{"location": "message", "role": "system"}])
    user_only = handler._estimate_cached_prefix_tokens("pineapple", "banana", [{"location": "message", "role": "user"}])

    # Caching the user message also caches the system message that precedes it, so its
    # estimate must be strictly larger than the system-only one.
    assert system_only == len("pineapple") // 2 + 1 + 32
    assert user_only == len("pineapple") // 2 + 1 + len("banana") // 2 + 1 + 48
    assert user_only > system_only


def test_estimate_is_zero_without_targeted_role(monkeypatch):
    handler = litellm_handler.LiteLLMAIHandler()
    assert handler._estimate_cached_prefix_tokens("pineapple", "banana", [{"location": "message", "role": "none"}]) == 0
    assert handler._estimate_cached_prefix_tokens("pineapple", "banana", []) == 0
