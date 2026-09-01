"""Streaming token-usage capture for streaming-required models.

Streaming completions used to return a MockResponse without `usage`, so
run-details token accounting silently skipped every streaming model. Usage is
now requested via stream_options (when the provider supports it), captured from
the final streamed chunk, and threaded through MockResponse.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_handler
from pr_agent.algo.ai_handlers.litellm_helpers import MockResponse, _handle_streaming_response

USAGE = SimpleNamespace(prompt_tokens=100, completion_tokens=40, total_tokens=140)


def _content_chunk(text, finish_reason=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text), finish_reason=finish_reason)],
    )


def _usage_chunk(usage):
    # The usage-bearing chunk (stream_options={"include_usage": True}) has empty choices
    return SimpleNamespace(choices=[], usage=usage)


async def _stream(chunks):
    for chunk in chunks:
        yield chunk


@pytest.mark.asyncio
async def test_handle_streaming_response_captures_final_chunk_usage():
    resp, finish_reason, usage = await _handle_streaming_response(_stream([
        _content_chunk("hello "),
        _content_chunk("world", finish_reason="stop"),
        _usage_chunk(USAGE),
    ]))

    assert resp == "hello world"
    assert finish_reason == "stop"
    assert usage is USAGE


@pytest.mark.asyncio
async def test_handle_streaming_response_usage_is_none_when_not_reported():
    resp, finish_reason, usage = await _handle_streaming_response(_stream([
        _content_chunk("hello", finish_reason="stop"),
    ]))

    assert resp == "hello"
    assert usage is None


def test_mock_response_exposes_usage_only_when_reported():
    with_usage = MockResponse("resp", "stop", USAGE)
    without_usage = MockResponse("resp", "stop")

    assert with_usage.usage is USAGE
    assert not hasattr(without_usage, "usage"), \
        "absent usage must stay absent so hasattr-based accounting skips it"


def test_request_streaming_usage_sets_stream_options_when_supported(monkeypatch):
    monkeypatch.setattr(
        litellm_handler.litellm, "get_supported_openai_params",
        lambda model: ["stream", "stream_options", "max_tokens"],
    )
    kwargs = {}

    litellm_handler.LiteLLMAIHandler._request_streaming_usage("some-model", kwargs)

    assert kwargs["stream_options"] == {"include_usage": True}


def test_request_streaming_usage_skips_when_unsupported(monkeypatch):
    monkeypatch.setattr(
        litellm_handler.litellm, "get_supported_openai_params",
        lambda model: ["stream", "max_tokens"],
    )
    kwargs = {}

    litellm_handler.LiteLLMAIHandler._request_streaming_usage("some-model", kwargs)

    assert "stream_options" not in kwargs, \
        "unsupported providers must not receive stream_options (they may reject it)"


def test_request_streaming_usage_survives_probe_failure(monkeypatch):
    def exploding(model):
        raise RuntimeError("unknown model")

    monkeypatch.setattr(litellm_handler.litellm, "get_supported_openai_params", exploding)
    kwargs = {}

    litellm_handler.LiteLLMAIHandler._request_streaming_usage("some-model", kwargs)  # must not raise

    assert "stream_options" not in kwargs


@pytest.mark.asyncio
async def test_get_completion_streaming_threads_usage_into_response(monkeypatch):
    handler = litellm_handler.LiteLLMAIHandler.__new__(litellm_handler.LiteLLMAIHandler)
    handler.streaming_required_models = ["streaming-model"]
    monkeypatch.setattr(
        litellm_handler.litellm, "get_supported_openai_params",
        lambda model: ["stream_options"],
    )

    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _stream([
            _content_chunk("streamed text", finish_reason="stop"),
            _usage_chunk(USAGE),
        ])

        resp, finish_reason, response_obj = await handler._get_completion(
            model="streaming-model", messages=[],
        )

    assert mock_call.call_args.kwargs["stream_options"] == {"include_usage": True}
    assert (resp, finish_reason) == ("streamed text", "stop")
    assert response_obj.usage is USAGE, \
        "usage must reach the response object so run details record it"
