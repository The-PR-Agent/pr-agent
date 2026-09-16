from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import openai
import pytest

from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.ai_handlers.litellm_helpers import (
    AssistantTurn,
    MockResponse,
    ToolCall,
    _extract_tool_calls,
    _handle_streaming_response,
)


def test_tool_call_dataclass():
    tc = ToolCall(id="call_123", type="function", name="read_pr_file", arguments='{"path": "foo.py"}')
    assert tc.id == "call_123"
    assert tc.type == "function"
    assert tc.name == "read_pr_file"
    assert tc.arguments == '{"path": "foo.py"}'


def test_assistant_turn_dataclass():
    turn = AssistantTurn(content="Hello", finish_reason="stop")
    assert turn.content == "Hello"
    assert turn.finish_reason == "stop"
    assert not turn.has_tool_calls

    tc = ToolCall(id="call_1", type="function", name="read_pr_file", arguments='{"path": "a.py"}')
    turn_with_tools = AssistantTurn(content="", finish_reason="tool_calls", tool_calls=[tc])
    assert turn_with_tools.has_tool_calls
    assert len(turn_with_tools.tool_calls) == 1
    assert turn_with_tools.tool_calls[0].name == "read_pr_file"


def test_mock_response_with_tool_calls():
    tc = ToolCall(id="call_1", type="function", name="read_pr_file", arguments='{"path": "a.py"}')
    mock_resp = MockResponse(resp="", finish_reason="tool_calls", model="gpt-4o", tool_calls=[tc])
    data = mock_resp.dict()
    assert data["model"] == "gpt-4o"
    msg = data["choices"][0]["message"]
    assert msg["content"] == ""
    assert len(msg["tool_calls"]) == 1
    assert msg["tool_calls"][0]["id"] == "call_1"
    assert msg["tool_calls"][0]["function"]["name"] == "read_pr_file"


def test_extract_tool_calls_from_dict_and_mock_response():
    # Empty / None
    assert _extract_tool_calls(None) == []
    assert _extract_tool_calls({}) == []

    # Non-streaming dict response
    raw_dict = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_abc",
                            "type": "function",
                            "function": {"name": "read_pr_file", "arguments": '{"path": "src/main.py"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    extracted = _extract_tool_calls(raw_dict)
    assert len(extracted) == 1
    assert extracted[0].id == "call_abc"
    assert extracted[0].name == "read_pr_file"
    assert extracted[0].arguments == '{"path": "src/main.py"}'

    # MockResponse with tool calls
    tc = ToolCall(id="call_mock", type="function", name="read_pr_file", arguments='{"path": "b.py"}')
    mock_resp = MockResponse(resp="", finish_reason="tool_calls", tool_calls=[tc])
    extracted_mock = _extract_tool_calls(mock_resp.dict())
    assert len(extracted_mock) == 1
    assert extracted_mock[0].id == "call_mock"



@pytest.mark.asyncio
async def test_streaming_content_only():
    async def mock_stream():
        chunks = [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="Hello", tool_calls=None),
                        finish_reason=None,
                    )
                ]
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=" world", tool_calls=None),
                        finish_reason="stop",
                    )
                ]
            ),
        ]
        for c in chunks:
            yield c

    content, finish_reason, mock_resp = await _handle_streaming_response(mock_stream(), model="test-model")
    assert content == "Hello world"
    assert finish_reason == "stop"
    tool_calls = _extract_tool_calls(mock_resp.dict())
    assert tool_calls == []


@pytest.mark.asyncio
async def test_streaming_tool_call_only_succeeds_without_content():
    async def mock_stream():
        chunks = [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id="call_1",
                                    type="function",
                                    function=SimpleNamespace(name="read_pr_file", arguments='{"path":'),
                                )
                            ],
                        ),
                        finish_reason=None,
                    )
                ]
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content=None,
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id=None,
                                    type=None,
                                    function=SimpleNamespace(name=None, arguments=' "test.py"}'),
                                )
                            ],
                        ),
                        finish_reason="tool_calls",
                    )
                ]
            ),
        ]
        for c in chunks:
            yield c

    content, finish_reason, mock_resp = await _handle_streaming_response(mock_stream(), model="test-model")
    assert content == ""
    assert finish_reason == "tool_calls"
    tool_calls = _extract_tool_calls(mock_resp.dict())
    assert len(tool_calls) == 1
    assert tool_calls[0].id == "call_1"
    assert tool_calls[0].name == "read_pr_file"
    assert tool_calls[0].arguments == '{"path": "test.py"}'


@pytest.mark.asyncio
async def test_streaming_empty_raises_apierror():
    async def mock_empty_stream():
        if False:
            yield

    with pytest.raises(openai.APIError):
        await _handle_streaming_response(mock_empty_stream(), model="test-model")


@pytest.mark.asyncio
async def test_get_completion_non_streaming_tool_call():
    handler = LiteLLMAIHandler.__new__(LiteLLMAIHandler)
    handler.azure = False
    handler._force_streaming_for_request = MagicMock(return_value=False)
    handler._requires_streaming = MagicMock(return_value=False)

    mock_resp = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_tool_1",
                            "type": "function",
                            "function": {"name": "read_pr_file", "arguments": '{"path": "lib.py"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    handler._acompletion = AsyncMock(return_value=mock_resp)

    content, finish_reason, resp_obj = await handler._get_completion(model="gpt-4o")
    assert content is None
    assert finish_reason == "tool_calls"
    tool_calls = _extract_tool_calls(resp_obj)
    assert len(tool_calls) == 1
    assert tool_calls[0].name == "read_pr_file"


@pytest.mark.asyncio
async def test_get_completion_empty_raises():
    handler = LiteLLMAIHandler.__new__(LiteLLMAIHandler)
    handler.azure = False
    handler._force_streaming_for_request = MagicMock(return_value=False)
    handler._requires_streaming = MagicMock(return_value=False)
    handler._acompletion = AsyncMock(return_value={"choices": [{"message": {"content": ""}, "finish_reason": None}]})

    with pytest.raises(openai.APIError):
        await handler._get_completion(model="gpt-4o")


def test_supports_tool_calling():
    handler = LiteLLMAIHandler.__new__(LiteLLMAIHandler)
    handler.azure = False
    handler._custom_llm_provider = None
    handler._route_model_for_request = MagicMock(return_value="gpt-4o")

    with patch("litellm.get_supported_openai_params", return_value=["tools", "temperature"]):
        assert handler.supports_tool_calling("gpt-4o")

    with patch("litellm.get_supported_openai_params", return_value=["temperature"]):
        assert not handler.supports_tool_calling("some-model")

    with patch("litellm.get_supported_openai_params", side_effect=Exception("error")):
        assert not handler.supports_tool_calling("broken-model")


@pytest.mark.asyncio
async def test_chat_completion_unwraps_turn():
    handler = LiteLLMAIHandler.__new__(LiteLLMAIHandler)
    handler.azure = False
    turn = AssistantTurn(content="answer text", finish_reason="stop")
    handler._chat_completion_with_retry = AsyncMock(return_value=turn)

    res, finish_reason = await handler.chat_completion(model="gpt-4o", system="sys", user="usr")
    assert res == "answer text"
    assert finish_reason == "stop"
    handler._chat_completion_with_retry.assert_awaited_once_with(
        "gpt-4o", "sys", "usr", 0.2, None, configured_deployment_id=None
    )


@pytest.mark.asyncio
async def test_chat_completion_with_tools_returns_turn():
    handler = LiteLLMAIHandler.__new__(LiteLLMAIHandler)
    handler.azure = False
    tc = ToolCall(id="c1", type="function", name="read_pr_file", arguments='{"path": "x.py"}')
    turn = AssistantTurn(content="", finish_reason="tool_calls", tool_calls=[tc])
    handler._chat_completion_with_retry = AsyncMock(return_value=turn)

    tools = [{"type": "function", "function": {"name": "read_pr_file"}}]
    result_turn = await handler.chat_completion_with_tools(
        model="gpt-4o", system="sys", user="usr", tools=tools
    )
    assert result_turn is turn
    assert result_turn.has_tool_calls
    handler._chat_completion_with_retry.assert_awaited_once_with(
        "gpt-4o", "sys", "usr", 0.2, None, configured_deployment_id=None, tools=tools, messages=None
    )
