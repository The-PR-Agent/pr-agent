from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


@pytest.mark.asyncio
async def test_chat_completion_passes_seed_when_temperature_is_zero(monkeypatch):
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: FakeSettings(config_values={"seed": 123}))

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()

        await handler.chat_completion(model="gpt-4o", system="sys", user="usr", temperature=0)

    assert mock_call.call_args.kwargs["seed"] == 123


@pytest.mark.asyncio
async def test_chat_completion_combines_prompts_for_user_message_only_models(monkeypatch):
    monkeypatch.setattr(litellm_handler, "get_settings", FakeSettings)

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()
        handler.user_message_only_models = ["user-only-model"]

        await handler.chat_completion(model="user-only-model", system="sys", user="usr")

    messages = mock_call.call_args.kwargs["messages"]
    assert messages == [{"role": "user", "content": "sys\n\n\nusr"}]


@pytest.mark.asyncio
async def test_get_completion_uses_streaming_for_required_models():
    handler = litellm_handler.LiteLLMAIHandler.__new__(litellm_handler.LiteLLMAIHandler)
    handler.streaming_required_models = ["streaming-model"]

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call, \
            patch("pr_agent.algo.ai_handlers.litellm_ai_handler._handle_streaming_response",
                  new_callable=AsyncMock) as mock_stream:
        mock_call.return_value = "stream"
        mock_stream.return_value = ("streamed text", "stop")

        resp, finish_reason, response_obj = await handler._get_completion(
            model="streaming-model",
            messages=[],
        )

    assert mock_call.call_args.kwargs["stream"] is True
    assert resp == "streamed text"
    assert finish_reason == "stop"
    assert response_obj.dict()["choices"][0]["message"]["content"] == "streamed text"
