from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import openai
import pytest

import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_handler
from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_helpers import (
    AssistantTurn,
    MockResponse,
    ToolCall,
    _handle_streaming_response,
    _handle_structured_streaming_response,
)


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
            temperature=0.2,
        )
        self.litellm = FakeBox()
        self._settings_values = {
            "aws.AWS_ACCESS_KEY_ID": "test-access-key",
            "aws.AWS_SECRET_ACCESS_KEY": "test-secret-key",
            "aws.AWS_REGION_NAME": "us-east-1",
            **(settings_values or {}),
        }

    def get(self, key, default=None):
        return self._settings_values.get(key, default)


# ---------------------------------------------------------------------------
# Dataclass tests
# ---------------------------------------------------------------------------


def test_tool_call_dataclass():
    tc = ToolCall(id="call_123", name="read_pr_file", arguments='{"path": "foo.py"}')
    assert tc.id == "call_123"
    assert tc.name == "read_pr_file"
    assert tc.arguments == '{"path": "foo.py"}'
    assert tc.type == "function"


def test_assistant_turn_dataclass():
    turn_empty = AssistantTurn()
    assert turn_empty.content is None
    assert turn_empty.finish_reason is None
    assert turn_empty.tool_calls == []
    assert not turn_empty.has_tool_calls

    turn_with_tools = AssistantTurn(
        content=None,
        tool_calls=[ToolCall(id="c1", name="read_pr_file", arguments="{}")],
    )
    assert turn_with_tools.has_tool_calls
    assert len(turn_with_tools.tool_calls) == 1


# ---------------------------------------------------------------------------
# MockResponse tests
# ---------------------------------------------------------------------------


def test_mock_response_with_tool_calls():
    tool_calls = [ToolCall(id="c1", name="read_pr_file", arguments='{"path": "test.py"}')]
    resp = MockResponse(resp="", finish_reason="tool_calls", model="gpt-4o", tool_calls=tool_calls)
    d = resp.dict()
    choice = d["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] == ""
    assert choice["message"]["tool_calls"] == [
        {
            "id": "c1",
            "type": "function",
            "function": {"name": "read_pr_file", "arguments": '{"path": "test.py"}'},
        }
    ]


# ---------------------------------------------------------------------------
# Streaming response handling
# ---------------------------------------------------------------------------


async def _async_stream(chunks):
    for chunk in chunks:
        yield chunk


def _make_chunk(content=None, tool_calls=None, finish_reason=None):
    delta = SimpleNamespace()
    if content is not None:
        delta.content = content
    if tool_calls is not None:
        delta.tool_calls = tool_calls
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice])


@pytest.mark.asyncio
async def test_structured_streaming_content_only():
    chunks = [
        _make_chunk(content="Hello "),
        _make_chunk(content="world!"),
        _make_chunk(finish_reason="stop"),
    ]
    turn, mock_resp = await _handle_structured_streaming_response(_async_stream(chunks), model="gpt-4o")
    assert turn.content == "Hello world!"
    assert not turn.has_tool_calls
    assert turn.finish_reason == "stop"
    assert mock_resp.dict()["choices"][0]["message"]["content"] == "Hello world!"


@pytest.mark.asyncio
async def test_structured_streaming_tool_call_only_succeeds_without_content():
    # Empty content with tool_calls should NOT raise APIError
    tc_chunk_0 = SimpleNamespace(
        index=0,
        id="call_abc",
        type="function",
        function=SimpleNamespace(name="read_pr_file", arguments='{"path": '),
    )
    tc_chunk_1 = SimpleNamespace(
        index=0,
        id=None,
        type=None,
        function=SimpleNamespace(name=None, arguments='"main.py"}'),
    )
    chunks = [
        _make_chunk(tool_calls=[tc_chunk_0]),
        _make_chunk(tool_calls=[tc_chunk_1]),
        _make_chunk(finish_reason="tool_calls"),
    ]
    turn, mock_resp = await _handle_structured_streaming_response(_async_stream(chunks), model="gpt-4o")
    assert turn.content is None
    assert turn.has_tool_calls
    assert len(turn.tool_calls) == 1
    call = turn.tool_calls[0]
    assert call.id == "call_abc"
    assert call.name == "read_pr_file"
    assert call.arguments == '{"path": "main.py"}'
    assert turn.finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_structured_streaming_content_and_tool_call():
    tc_chunk = SimpleNamespace(
        index=0,
        id="call_xyz",
        type="function",
        function=SimpleNamespace(name="read_pr_file", arguments='{"path": "app.py"}'),
    )
    chunks = [
        _make_chunk(content="Let me check that file."),
        _make_chunk(tool_calls=[tc_chunk]),
        _make_chunk(finish_reason="tool_calls"),
    ]
    turn, mock_resp = await _handle_structured_streaming_response(_async_stream(chunks), model="gpt-4o")
    assert turn.content == "Let me check that file."
    assert turn.has_tool_calls
    assert turn.tool_calls[0].name == "read_pr_file"


@pytest.mark.asyncio
async def test_structured_streaming_empty_raises_apierror():
    # Both content and tool_calls empty must raise APIError
    chunks = [
        _make_chunk(content=""),
        _make_chunk(finish_reason="stop"),
    ]
    with pytest.raises(openai.APIError) as exc_info:
        await _handle_structured_streaming_response(_async_stream(chunks), model="gpt-4o")
    assert "no content received" in str(exc_info.value)


@pytest.mark.asyncio
async def test_legacy_streaming_helper_contract_preserved():
    # _handle_streaming_response must still return (str, str, MockResponse)
    chunks = [
        _make_chunk(content="Answer text"),
        _make_chunk(finish_reason="stop"),
    ]
    result = await _handle_streaming_response(_async_stream(chunks), model="gpt-4o")
    assert isinstance(result, tuple)
    assert len(result) == 3
    full_resp, finish_reason, mock_resp = result
    assert full_resp == "Answer text"
    assert finish_reason == "stop"
    assert isinstance(mock_resp, MockResponse)


# ---------------------------------------------------------------------------
# supports_tool_calling
# ---------------------------------------------------------------------------


def test_base_ai_handler_defaults_to_false():
    class DummyHandler(BaseAiHandler):
        def __init__(self):
            pass

        @property
        def deployment_id(self):
            return None

        async def chat_completion(self, model, system, user, temperature=0.2, img_path=None):
            return "ok", "stop"

    dummy = DummyHandler()
    assert dummy.supports_tool_calling("gpt-4o") is False


def test_litellm_handler_supports_tool_calling(monkeypatch):
    handler = litellm_handler.LiteLLMAIHandler.__new__(litellm_handler.LiteLLMAIHandler)
    handler._custom_llm_provider = None

    with patch("litellm.get_supported_openai_params", return_value=["tools", "temperature"]):
        assert handler.supports_tool_calling("gpt-4o") is True

    with patch("litellm.get_supported_openai_params", return_value=["temperature"]):
        assert handler.supports_tool_calling("gpt-3.5-turbo") is False

    with patch("litellm.get_supported_openai_params", return_value=None):
        assert handler.supports_tool_calling("unknown-model") is False

    with patch("litellm.get_supported_openai_params", side_effect=Exception("network err")):
        assert handler.supports_tool_calling("error-model") is False


# ---------------------------------------------------------------------------
# _get_completion_with_tools & chat_completion_with_tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_completion_with_tools_non_streaming(monkeypatch):
    monkeypatch.setattr(litellm_handler, "get_settings", FakeSettings)
    handler = litellm_handler.LiteLLMAIHandler.__new__(litellm_handler.LiteLLMAIHandler)
    handler._custom_llm_provider = None
    handler._force_streaming_for_request = lambda provider, base: False
    handler._requires_streaming = lambda model: False

    mock_resp = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "read_pr_file", "arguments": '{"path": "x.py"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    handler._acompletion = AsyncMock(return_value=mock_resp)

    turn, response_obj = await handler._get_completion_with_tools(model="gpt-4o")
    assert turn.content is None
    assert turn.has_tool_calls
    assert turn.tool_calls[0].id == "call_1"
    assert turn.tool_calls[0].name == "read_pr_file"
    assert turn.tool_calls[0].arguments == '{"path": "x.py"}'
    assert response_obj == mock_resp


@pytest.mark.asyncio
async def test_get_completion_with_tools_empty_raises():
    handler = litellm_handler.LiteLLMAIHandler.__new__(litellm_handler.LiteLLMAIHandler)
    handler._custom_llm_provider = None
    handler._force_streaming_for_request = lambda provider, base: False
    handler._requires_streaming = lambda model: False

    mock_resp = {
        "choices": [
            {
                "message": {"content": None, "tool_calls": None},
                "finish_reason": "stop",
            }
        ]
    }
    handler._acompletion = AsyncMock(return_value=mock_resp)

    with pytest.raises(openai.APIError):
        await handler._get_completion_with_tools(model="gpt-4o")


@pytest.mark.asyncio
async def test_chat_completion_with_tools_preserves_messages(monkeypatch):
    monkeypatch.setattr(litellm_handler, "get_settings", FakeSettings)

    handler = litellm_handler.LiteLLMAIHandler()
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "usr"},
        {"role": "assistant", "content": "thinking..."},
        {"role": "tool", "tool_call_id": "c1", "content": '{"path": "foo.py"}'},
    ]

    expected_turn = AssistantTurn(content="Done!", finish_reason="stop")
    handler._chat_completion_with_tools_retry = AsyncMock(return_value=expected_turn)

    res = await handler.chat_completion_with_tools(
        model="gpt-4o",
        messages=messages,
    )
    assert res == expected_turn
    handler._chat_completion_with_tools_retry.assert_called_once()
    call_kwargs = handler._chat_completion_with_tools_retry.call_args.kwargs
    assert call_kwargs["messages"] == messages
