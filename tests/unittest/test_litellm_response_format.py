"""`litellm.response_format` - opt-in JSON grammar enforcement for small self-hosted models."""

from unittest.mock import AsyncMock, patch

import openai
import pytest

import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_handler
from tests.unittest.test_litellm_chat_completion_core import FakeSettings, _mock_response


def _settings(response_format):
    settings = FakeSettings()
    settings.litellm.response_format = response_format
    return settings


@pytest.mark.asyncio
async def test_json_object_is_forwarded_as_an_openai_response_format(monkeypatch):
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _settings("json_object"))
    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as call:
        call.return_value = _mock_response()
        await litellm_handler.LiteLLMAIHandler().chat_completion(model="gpt-4o", system="s", user="u")
    assert call.call_args.kwargs["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_the_parameter_is_absent_by_default(monkeypatch):
    monkeypatch.setattr(litellm_handler, "get_settings", FakeSettings)
    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as call:
        call.return_value = _mock_response()
        await litellm_handler.LiteLLMAIHandler().chat_completion(model="gpt-4o", system="s", user="u")
    assert "response_format" not in call.call_args.kwargs


@pytest.mark.asyncio
async def test_an_unsupported_value_is_rejected_rather_than_sent(monkeypatch):
    """A typo here would otherwise reach the provider and fail with its error, not ours.

    The value is validated above the request block, so it surfaces as the ValueError it is. Were
    it raised inside that block the handler would wrap it as openai.APIError, which
    _should_retry_same_model retries - replaying an unfixable config error on every model.
    """
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _settings("json_schema"))
    with (
        patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as call,
        pytest.raises(ValueError, match="response_format must be 'json_object'"),
    ):
        await litellm_handler.LiteLLMAIHandler().chat_completion(model="gpt-4o", system="s", user="u")
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_config_error_is_not_retried_on_the_same_model(monkeypatch):
    """The retry predicate must decline it: retrying a typo burns every model's latency."""
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: _settings("json_schema"))
    handler = litellm_handler.LiteLLMAIHandler()
    with patch.object(handler, "_resolve_response_format",
                      side_effect=ValueError("boom")) as resolve:
        with pytest.raises(ValueError):
            await handler.chat_completion(model="gpt-4o", system="s", user="u")
    assert resolve.call_count == 1
    assert not litellm_handler._should_retry_same_model(ValueError("boom"))
    assert litellm_handler._should_retry_same_model(
        openai.APIError("x", request=None, body=None))


def test_json_output_is_read_by_the_yaml_loader_unchanged():
    """The whole point: nothing downstream has to change for a JSON response."""
    from pr_agent.algo.utils import load_yaml
    response = ('{"review": {"key_issues_to_review": [{"relevant_file": "a.py", "issue_header": "Bug",'
                ' "issue_content": "line one\\nline two", "start_line": 3, "end_line": 4}],'
                ' "security_concerns": "No"}}')
    data = load_yaml(response, first_key="review", last_key="security_concerns")
    assert data["review"]["key_issues_to_review"][0]["issue_content"] == "line one\nline two"
