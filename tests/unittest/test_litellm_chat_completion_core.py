from unittest.mock import AsyncMock, MagicMock, patch

import openai
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


def _mock_response(usage=None):
    mock = MagicMock()
    response = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
    if usage is not None:
        response["usage"] = usage
        # run_details reads the litellm attribute, not the dict form
        mock.usage = usage
    else:
        mock.usage = None
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
@pytest.mark.parametrize(
    ("model", "expected_model_id"),
    [
        ("bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0", "profile-123"),
        ("bedrock_mantle/xai.grok-4.3", None),
    ],
)
async def test_chat_completion_scopes_model_id_to_classic_bedrock(monkeypatch, model, expected_model_id):
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: FakeSettings(settings_values={"litellm.model_id": "profile-123"}),
    )

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()

        await handler.chat_completion(model=model, system="sys", user="usr")

    if expected_model_id is None:
        assert "model_id" not in mock_call.call_args.kwargs
    else:
        assert mock_call.call_args.kwargs["model_id"] == expected_model_id


@pytest.mark.asyncio
async def test_chat_completion_accumulates_usage_into_run_details(monkeypatch):
    from pr_agent.algo.run_details import init_run_details

    monkeypatch.setattr(litellm_handler, "get_settings", FakeSettings)
    usage = {"prompt_tokens": 101, "completion_tokens": 23, "total_tokens": 124}

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response(usage)
        handler = litellm_handler.LiteLLMAIHandler()

        details = init_run_details()
        await handler.chat_completion(model="gpt-4o", system="sys", user="usr")

    assert details.prompt_tokens == 101
    assert details.completion_tokens == 23
    assert details.total_tokens == 124


@pytest.mark.asyncio
async def test_chat_completion_rejects_seed_for_claude_opus_4_8_default_temperature(monkeypatch):
    class FakeAPIError(Exception):
        # same signature as openai.APIError, which the handler constructs with a message
        def __init__(self, message="", request=None, body=None):
            super().__init__(message)
            self.request = request
            self.body = body

    monkeypatch.setattr(litellm_handler, "get_settings", lambda: FakeSettings(config_values={"seed": 123}))
    monkeypatch.setattr(litellm_handler.openai, "APIError", FakeAPIError)

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        handler = litellm_handler.LiteLLMAIHandler()

        with pytest.raises(FakeAPIError) as exc_info:
            await handler.chat_completion(model="claude-opus-4-8", system="sys", user="usr")

    assert isinstance(exc_info.value.__cause__, ValueError)
    assert str(exc_info.value.__cause__) == "Seed (123) is not supported with temperature (0.2) > 0"
    mock_call.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    [
        "anthropic/claude-opus-4-8",
        "claude-opus-4-8",
        "vertex_ai/claude-opus-4-8",
        "bedrock/anthropic.claude-opus-4-8",
        "bedrock/global.anthropic.claude-opus-4-8",
        "bedrock/us.anthropic.claude-opus-4-8",
        "bedrock/eu.anthropic.claude-opus-4-8",
        "bedrock/au.anthropic.claude-opus-4-8",
        "bedrock/jp.anthropic.claude-opus-4-8",
    ],
)
async def test_chat_completion_strips_temperature_for_claude_opus_4_8(monkeypatch, model):
    monkeypatch.setattr(litellm_handler, "get_settings", FakeSettings)

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()

        await handler.chat_completion(model=model, system="sys", user="usr", temperature=0.2)

    assert "temperature" not in mock_call.call_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    [
        "anthropic/claude-opus-5",
        "claude-opus-5",
        "vertex_ai/claude-opus-5",
        "bedrock/anthropic.claude-opus-5",
        "bedrock/global.anthropic.claude-opus-5",
        "bedrock/us.anthropic.claude-opus-5",
        "bedrock/eu.anthropic.claude-opus-5",
        "bedrock/au.anthropic.claude-opus-5",
        "bedrock/jp.anthropic.claude-opus-5",
    ],
)
async def test_chat_completion_strips_temperature_for_claude_opus_5(monkeypatch, model):
    monkeypatch.setattr(litellm_handler, "get_settings", FakeSettings)

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()

        await handler.chat_completion(model=model, system="sys", user="usr", temperature=0.2)

    assert "temperature" not in mock_call.call_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    [
        "anthropic/claude-sonnet-5",
        "claude-sonnet-5",
        "vertex_ai/claude-sonnet-5",
        "bedrock/anthropic.claude-sonnet-5",
        "bedrock/global.anthropic.claude-sonnet-5",
        "bedrock/us.anthropic.claude-sonnet-5",
        "bedrock/au.anthropic.claude-sonnet-5",
        "bedrock/eu.anthropic.claude-sonnet-5",
        "bedrock/jp.anthropic.claude-sonnet-5",
    ],
)
async def test_chat_completion_strips_temperature_for_claude_sonnet_5(monkeypatch, model):
    monkeypatch.setattr(litellm_handler, "get_settings", FakeSettings)

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()

        await handler.chat_completion(model=model, system="sys", user="usr", temperature=0.2)

    assert "temperature" not in mock_call.call_args.kwargs


@pytest.mark.asyncio
async def test_chat_completion_does_not_use_extended_thinking_for_claude_opus_4_8(monkeypatch):
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: FakeSettings(config_values={"enable_claude_extended_thinking": True}),
    )

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()

        await handler.chat_completion(model="claude-opus-4-8", system="sys", user="usr", temperature=0.2)

    assert "thinking" not in mock_call.call_args.kwargs
    assert "max_tokens" not in mock_call.call_args.kwargs
    assert "temperature" not in mock_call.call_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    [
        "anthropic/claude-opus-5",
        "claude-opus-5",
        "vertex_ai/claude-opus-5",
        "bedrock/anthropic.claude-opus-5",
        "bedrock/global.anthropic.claude-opus-5",
        "bedrock/us.anthropic.claude-opus-5",
        "bedrock/eu.anthropic.claude-opus-5",
        "bedrock/au.anthropic.claude-opus-5",
        "bedrock/jp.anthropic.claude-opus-5",
    ],
)
async def test_chat_completion_does_not_use_extended_thinking_for_claude_opus_5(monkeypatch, model):
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: FakeSettings(config_values={"enable_claude_extended_thinking": True}),
    )

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()

        await handler.chat_completion(model=model, system="sys", user="usr", temperature=0.2)

    assert "thinking" not in mock_call.call_args.kwargs
    assert "max_tokens" not in mock_call.call_args.kwargs
    assert "temperature" not in mock_call.call_args.kwargs


@pytest.mark.asyncio
async def test_chat_completion_does_not_use_extended_thinking_for_claude_sonnet_5(monkeypatch):
    monkeypatch.setattr(
        litellm_handler,
        "get_settings",
        lambda: FakeSettings(config_values={"enable_claude_extended_thinking": True}),
    )

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()

        await handler.chat_completion(model="claude-sonnet-5", system="sys", user="usr", temperature=0.2)

    assert "thinking" not in mock_call.call_args.kwargs
    assert "max_tokens" not in mock_call.call_args.kwargs
    assert "temperature" not in mock_call.call_args.kwargs


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
        completed_response = MagicMock()
        completed_response.dict.return_value = {
            "choices": [{"message": {"content": "streamed text"}, "finish_reason": "stop"}]
        }
        mock_stream.return_value = ("streamed text", "stop", completed_response)

        resp, finish_reason, response_obj = await handler._get_completion(
            model="streaming-model",
            messages=[],
        )

    assert mock_call.call_args.kwargs["stream"] is True
    assert mock_call.call_args.kwargs["stream_options"] == {"include_usage": True}
    mock_stream.assert_awaited_once_with("stream", model="streaming-model")
    assert resp == "streamed text"
    assert finish_reason == "stop"
    assert response_obj.dict()["choices"][0]["message"]["content"] == "streamed text"


def _empty_content_response(finish_reason="stop"):
    mock = MagicMock()
    response = {"choices": [{"message": {"content": ""}, "finish_reason": finish_reason}]}
    mock.__getitem__.side_effect = response.__getitem__
    mock.dict.return_value = response
    return mock


@pytest.mark.asyncio
async def test_get_completion_raises_on_empty_content_for_non_streaming_model():
    # A reasoning model that puts everything into a thinking/reasoning block and leaves
    # `content` empty is not caught by the "response is None or no choices" guard, so an
    # empty response must be treated as a failure instead of silently returned.
    handler = litellm_handler.LiteLLMAIHandler.__new__(litellm_handler.LiteLLMAIHandler)
    handler.streaming_required_models = []

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _empty_content_response(finish_reason="stop")

        with pytest.raises(openai.APIError):
            await handler._get_completion(model="anthropic/custom-reasoning-model", messages=[])


@pytest.mark.asyncio
async def test_get_completion_returns_non_empty_content_for_non_streaming_model():
    handler = litellm_handler.LiteLLMAIHandler.__new__(litellm_handler.LiteLLMAIHandler)
    handler.streaming_required_models = []

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()

        resp, finish_reason, response_obj = await handler._get_completion(model="gpt-4o", messages=[])

    assert resp == "ok"
    assert finish_reason == "stop"
