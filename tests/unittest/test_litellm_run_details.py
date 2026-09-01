import asyncio

import pytest

from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.ai_handlers.litellm_helpers import MockResponse
from pr_agent.algo.run_details import get_run_details, init_run_details


class _Usage:
    def __init__(self, prompt_tokens, completion_tokens, total_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


class _Response:
    """Minimal stand-in for a litellm response object."""

    def __init__(self, usage):
        self.usage = usage

    def dict(self):
        return {"choices": [{"message": {"content": "resp"}, "finish_reason": "stop"}]}


def test_record_completion_metadata_accumulates_usage():
    init_run_details()

    LiteLLMAIHandler._record_completion_metadata(_Response(_Usage(100, 10, 110)))
    LiteLLMAIHandler._record_completion_metadata(_Response(_Usage(50, 5, 55)))

    details = get_run_details()
    assert details.num_ai_calls == 2
    assert details.prompt_tokens == 150
    assert details.completion_tokens == 15
    assert details.total_tokens == 165


def test_record_completion_metadata_counts_streaming_calls_without_tokens():
    init_run_details()

    LiteLLMAIHandler._record_completion_metadata(MockResponse("resp", "stop"))

    details = get_run_details()
    assert details.num_ai_calls == 1
    assert details.has_token_usage is False


def test_record_completion_metadata_tolerates_missing_response():
    init_run_details()

    LiteLLMAIHandler._record_completion_metadata(None)

    details = get_run_details()
    assert details.num_ai_calls == 1
    assert details.has_token_usage is False


def _bare_handler():
    """Build a handler without __init__, which would demand real provider credentials."""
    handler = LiteLLMAIHandler.__new__(LiteLLMAIHandler)
    handler.azure = False
    handler.api_base = None
    handler.repetition_penalty = None
    handler.add_litellm_callbacks = False
    handler.claude_extended_thinking_models = []
    handler.no_support_temperature_models = []
    handler.support_reasoning_models = []
    handler.user_message_only_models = []
    handler._aws_imds_mode = False
    handler._aws_imds_fell_back = False
    handler._aws_static_creds = None
    handler._aws_bedrock_lock = None
    return handler


@pytest.mark.asyncio
async def test_chat_completion_records_the_call_it_just_made(monkeypatch):
    """Guard the wiring, not just the recorder.

    litellm is the default handler, so if `chat_completion` stops calling
    `_record_completion_metadata` every counter silently drops to zero.
    """
    handler = _bare_handler()

    async def fake_get_completion(**_kwargs):
        return "resp", "stop", _Response(_Usage(100, 10, 110))

    monkeypatch.setattr(handler, "_get_completion", fake_get_completion)

    init_run_details()
    resp, finish_reason = await handler.chat_completion(model="some-model", system="sys", user="usr")

    details = get_run_details()
    assert (resp, finish_reason) == ("resp", "stop")
    assert details.num_ai_calls == 1
    assert details.prompt_tokens == 100
    assert details.completion_tokens == 10
    assert details.total_tokens == 110


@pytest.mark.asyncio
async def test_chat_completion_does_not_record_when_the_call_fails(monkeypatch):
    """A failed model must not be counted, or fallback runs would inflate the totals."""
    handler = _bare_handler()

    async def failing_get_completion(**_kwargs):
        raise ValueError("provider exploded")

    monkeypatch.setattr(handler, "_get_completion", failing_get_completion)

    init_run_details()
    with pytest.raises(Exception):
        await handler.chat_completion(model="some-model", system="sys", user="usr")

    details = get_run_details()
    assert details.num_ai_calls == 0
    assert details.has_token_usage is False


@pytest.mark.asyncio
async def test_concurrent_chat_completions_accumulate_into_one_collector(monkeypatch):
    """`/improve` fans out chunks with asyncio.gather; every chunk must be counted."""
    handler = _bare_handler()

    async def fake_get_completion(**_kwargs):
        await asyncio.sleep(0)
        return "resp", "stop", _Response(_Usage(10, 1, 11))

    monkeypatch.setattr(handler, "_get_completion", fake_get_completion)

    init_run_details()
    await asyncio.gather(*(
        handler.chat_completion(model="some-model", system="sys", user=f"usr-{i}")
        for i in range(3)
    ))

    details = get_run_details()
    assert details.num_ai_calls == 3
    assert details.total_tokens == 33
