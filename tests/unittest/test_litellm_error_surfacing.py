"""Regression tests for issue #2576: a misconfigured model looked like a silent failure.

Two separate defects destroyed the provider's error message before it reached the user:
  1. tenacity's @retry wrapped the real exception in RetryError, whose str() is only
     "RetryError[<Future ... raised BadRequestError>]" - the actionable text was gone.
  2. `raise openai.APIError` raised the CLASS, so Python's instantiation failed with
     "TypeError: __init__() missing 2 required positional arguments" and dropped __cause__.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
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


def _api_error(message):
    """A real openai.APIError, matching what litellm raises for bad model/provider config."""
    return openai.APIError(message, request=httpx.Request("POST", "http://test"), body=None)


# The message litellm produces when it cannot infer a provider - the exact case in issue #2576.
PROVIDER_ERROR = (
    "litellm.BadRequestError: LLM Provider NOT provided. Pass in the LLM provider you are "
    "trying to call. You passed model=gpt-5.6-terra Learn more: "
    "https://docs.litellm.ai/docs/providers"
)


@pytest.mark.asyncio
async def test_provider_error_survives_retry_instead_of_becoming_retryerror(monkeypatch):
    # Retries are exhausted, so tenacity must re-raise the provider's own exception. Without
    # reraise=True it raises RetryError, whose message names only the exception CLASS and drops
    # the model name and the reason - which is what made #2576 look like a silent failure.
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: FakeSettings())

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
               new_callable=AsyncMock) as mock_call:
        mock_call.side_effect = _api_error(PROVIDER_ERROR)
        handler = litellm_handler.LiteLLMAIHandler()

        with pytest.raises(openai.APIError) as exc_info:
            await handler.chat_completion(model="gpt-5.6-terra", system="sys", user="usr")

    assert not isinstance(exc_info.value, tenacity.RetryError)
    # The caller can see which model failed and why, not just an exception class name.
    assert "gpt-5.6-terra" in str(exc_info.value)
    assert "LLM Provider NOT provided" in str(exc_info.value)
    # It really did retry; the error survived the retry wrapper rather than bypassing it.
    assert mock_call.call_count == litellm_handler.MODEL_RETRIES


@pytest.mark.asyncio
async def test_empty_choices_raises_usable_api_error_not_typeerror(monkeypatch):
    # A response with no choices must surface as a real APIError carrying a message. Raising the
    # bare class instead produced "TypeError: missing 2 required positional arguments", which
    # tells the user nothing about what actually went wrong.
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: FakeSettings())

    empty = MagicMock()
    response = {"choices": []}
    empty.__getitem__.side_effect = response.__getitem__

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
               new_callable=AsyncMock) as mock_call:
        mock_call.return_value = empty
        handler = litellm_handler.LiteLLMAIHandler()

        with pytest.raises(openai.APIError) as exc_info:
            await handler.chat_completion(model="gpt-4o", system="sys", user="usr")

    assert not isinstance(exc_info.value, TypeError)
    assert str(exc_info.value)  # a non-empty, human-readable reason
    assert "gpt-4o" in str(exc_info.value)


@pytest.mark.asyncio
async def test_non_api_error_is_wrapped_but_keeps_message_and_cause(monkeypatch):
    # Non-APIError failures are re-raised as APIError so the retry/fallback logic can classify
    # them, but the original text and __cause__ must survive the conversion.
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: FakeSettings())

    original = ValueError("upstream exploded: bad endpoint")

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
               new_callable=AsyncMock) as mock_call:
        mock_call.side_effect = original
        handler = litellm_handler.LiteLLMAIHandler()

        with pytest.raises(openai.APIError) as exc_info:
            await handler.chat_completion(model="gpt-4o", system="sys", user="usr")

    assert not isinstance(exc_info.value, TypeError)
    assert "upstream exploded: bad endpoint" in str(exc_info.value)
    assert exc_info.value.__cause__ is original
