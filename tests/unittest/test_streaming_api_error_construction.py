"""Raise a real openai.APIError when a streaming response arrives empty."""
import asyncio

import openai
import pytest

from pr_agent.algo.ai_handlers.litellm_helpers import _handle_streaming_response


class Chunk:
    def __init__(self, content, finish_reason):
        delta = type("Delta", (), {"content": content})()
        self.choices = [type("Choice", (), {"delta": delta, "finish_reason": finish_reason})()]
        self.usage = None
        self._hidden_params = {}


class Stream:
    def __init__(self, chunks):
        self.chunks = chunks

    def __aiter__(self):
        async def generate():
            for chunk in self.chunks:
                yield chunk
        return generate()


def collect(chunks):
    return asyncio.run(_handle_streaming_response(Stream(chunks), model="some-model"))


def test_collect_a_normal_streaming_response():
    """Keep assembling a streamed answer exactly as before."""
    content, finish_reason, _ = collect([Chunk("hel", None), Chunk("lo", None),
                                         Chunk(None, "stop")])

    assert content == "hello"
    assert finish_reason == "stop"


@pytest.mark.parametrize("chunks, reason", [
    ([Chunk(None, "stop")], "completed with a finish reason but no content"),
    ([Chunk(None, None)], "ended without content or a finish reason"),
])
def test_raise_an_api_error_the_retry_can_catch(chunks, reason):
    """openai.APIError is what @retry(retry_if_exception_type(openai.APIError)) waits for."""
    with pytest.raises(openai.APIError):
        collect(chunks)


def test_the_raised_error_carries_its_message():
    """Keep the diagnostic message that names the finish reason."""
    with pytest.raises(openai.APIError) as excinfo:
        collect([Chunk(None, "content_filter")])

    assert "content_filter" in str(excinfo.value)
