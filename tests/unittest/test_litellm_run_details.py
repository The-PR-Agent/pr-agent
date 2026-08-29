import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
        return {
            "choices": [{"message": {"content": "resp"}, "finish_reason": "stop"}],
            "usage": self.usage,
        }


def _set_cost_collection(monkeypatch, enabled):
    settings = SimpleNamespace(
        get=lambda key, default=None: enabled if key == "config.output_run_cost" else default
    )
    monkeypatch.setattr("pr_agent.algo.ai_handlers.litellm_ai_handler.get_settings", lambda: settings)


def test_record_completion_metadata_accumulates_usage():
    init_run_details()

    LiteLLMAIHandler._record_completion_metadata(_Response(_Usage(100, 10, 110)))
    LiteLLMAIHandler._record_completion_metadata(_Response(_Usage(50, 5, 55)))

    details = get_run_details()
    assert details.num_ai_calls == 2
    assert details.prompt_tokens == 150
    assert details.completion_tokens == 15
    assert details.total_tokens == 165


def test_record_completion_metadata_collects_known_non_streaming_cost(monkeypatch):
    usage = _Usage(100, 10, 110)
    response = _Response(usage)
    _set_cost_collection(monkeypatch, True)
    init_run_details()

    with patch(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.litellm.completion_cost",
        return_value=0.0842,
    ) as completion_cost:
        LiteLLMAIHandler._record_completion_metadata(response, model="model-a")

    details = get_run_details()
    assert details.total_cost_usd == Decimal("0.0842")
    assert details.known_cost_call_count == 1
    assert details.cost_status == "complete"
    assert details.model_costs_usd == {"model-a": Decimal("0.0842")}
    assert completion_cost.call_args.kwargs["completion_response"]["usage"] is usage


def test_record_completion_metadata_prices_routed_model_and_records_configured_model(monkeypatch):
    usage = _Usage(100, 10, 110)
    response = _Response(usage)
    _set_cost_collection(monkeypatch, True)
    init_run_details()

    with patch(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.litellm.completion_cost",
        return_value=0.0842,
    ) as completion_cost:
        LiteLLMAIHandler._record_completion_metadata(
            response,
            model="azure/gpt-5",
            display_model="gpt-5_thinking",
        )

    assert completion_cost.call_args.kwargs["model"] == "azure/gpt-5"
    assert get_run_details().model_costs_usd == {"gpt-5_thinking": Decimal("0.0842")}


def test_record_completion_metadata_uses_positive_finalized_inline_cost(monkeypatch):
    _set_cost_collection(monkeypatch, True)
    init_run_details()
    response = {"usage": {"cost": "0.0031"}}

    with patch(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.litellm.completion_cost",
    ) as completion_cost:
        LiteLLMAIHandler._record_completion_metadata(response, model="model-a")

    details = get_run_details()
    assert details.total_cost_usd == Decimal("0.0031")
    assert details.known_cost_call_count == 1
    completion_cost.assert_not_called()


def test_zero_inline_cost_without_priceable_usage_stays_unavailable(monkeypatch):
    _set_cost_collection(monkeypatch, True)
    init_run_details()
    response = {"usage": {"response_cost": 0}}

    with patch(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.litellm.completion_cost",
    ) as completion_cost:
        LiteLLMAIHandler._record_completion_metadata(response, model="model-a")

    details = get_run_details()
    assert details.total_cost_usd == Decimal("0")
    assert details.known_cost_call_count == 0
    assert details.cost_status == "unavailable"
    completion_cost.assert_not_called()


def test_zero_token_usage_with_provider_timing_floats_stays_unavailable(monkeypatch):
    """Groq-style timing floats (queue_time, prompt_time) are not billable
    quantities and must not send zero-token usage to completion_cost."""
    _set_cost_collection(monkeypatch, True)
    init_run_details()
    response = {"usage": {"prompt_tokens": 0, "completion_tokens": 0,
                          "queue_time": 0.019, "prompt_time": 0.004}}

    with patch(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.litellm.completion_cost",
        return_value=0.0,
    ) as completion_cost:
        LiteLLMAIHandler._record_completion_metadata(response, model="model-a")

    details = get_run_details()
    assert details.known_cost_call_count == 0
    assert details.cost_status == "unavailable"
    completion_cost.assert_not_called()


def test_disabled_cost_collection_does_not_calculate_or_record_cost(monkeypatch):
    _set_cost_collection(monkeypatch, False)
    init_run_details()
    response = _Response(_Usage(100, 10, 110))

    with patch(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.litellm.completion_cost",
    ) as completion_cost:
        LiteLLMAIHandler._record_completion_metadata(response, model="model-a")

    details = get_run_details()
    assert details.num_ai_calls == 1
    assert details.known_cost_call_count == 0
    assert details.cost_status == "unavailable"
    completion_cost.assert_not_called()


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


def _streaming_handler():
    handler = LiteLLMAIHandler.__new__(LiteLLMAIHandler)
    handler.streaming_required_models = ["streaming-model"]
    handler.force_streaming_provider = ""
    handler.force_streaming_api_base_substrings = []
    return handler


async def _async_chunks(*chunks):
    for chunk in chunks:
        yield chunk


@pytest.mark.asyncio
async def test_streamed_completion_preserves_finalized_usage_and_collects_known_cost(monkeypatch):
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "cache_read_input_tokens": 80,
        "cache_creation_input_tokens": 10,
        "completion_tokens_details": {"reasoning_tokens": 8},
    }
    content_chunk = SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content="streamed"), finish_reason="stop")],
        usage=None,
    )
    usage_chunk = SimpleNamespace(choices=[], usage=usage)
    handler = _streaming_handler()
    _set_cost_collection(monkeypatch, True)

    with patch(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
        new_callable=AsyncMock,
    ) as acompletion, patch(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.litellm.completion_cost",
        return_value=0.071,
    ) as completion_cost:
        acompletion.return_value = _async_chunks(content_chunk, usage_chunk)
        init_run_details()
        resp, finish_reason, response = await handler._get_completion(
            model="streaming-model",
            messages=[{"role": "user", "content": "hello"}],
        )
        handler._record_completion_metadata(response, model="streaming-model")

    details = get_run_details()
    assert (resp, finish_reason) == ("streamed", "stop")
    assert response.usage is usage
    assert details.total_tokens == 120
    assert details.total_cost_usd == Decimal("0.071")
    assert details.cost_status == "complete"
    assert acompletion.call_args.kwargs["stream_options"] == {"include_usage": True}
    assert completion_cost.call_args.kwargs["completion_response"]["usage"] == usage


@pytest.mark.asyncio
async def test_streamed_completion_without_finalized_usage_marks_cost_unavailable(monkeypatch):
    content_chunk = SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content="streamed"), finish_reason="stop")],
        usage=None,
    )
    handler = _streaming_handler()
    _set_cost_collection(monkeypatch, True)

    with patch(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion",
        new_callable=AsyncMock,
    ) as acompletion, patch(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.litellm.completion_cost",
    ) as completion_cost:
        acompletion.return_value = _async_chunks(content_chunk)
        init_run_details()
        _, _, response = await handler._get_completion(model="streaming-model", messages=[])
        handler._record_completion_metadata(response, model="streaming-model")

    details = get_run_details()
    assert response.usage is None
    assert details.num_ai_calls == 1
    assert details.has_token_usage is False
    assert details.known_cost_call_count == 0
    assert details.cost_status == "unavailable"
    assert details.total_cost_usd == Decimal("0")
    completion_cost.assert_not_called()


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
async def test_chat_completion_preserves_configured_model_for_cost_breakdown(monkeypatch):
    handler = _bare_handler()
    handler.azure = True
    response = _Response(_Usage(100, 10, 110))
    routed_models = []

    async def fake_get_completion(**kwargs):
        routed_models.append(kwargs["model"])
        return "resp", "stop", response

    monkeypatch.setattr(handler, "_get_completion", fake_get_completion)

    with patch.object(
        handler,
        "_record_completion_metadata",
        wraps=handler._record_completion_metadata,
    ) as record_completion_metadata:
        init_run_details()
        await handler.chat_completion(model="gpt-4.1", system="sys", user="usr")

    assert routed_models == ["azure/gpt-4.1"]
    record_completion_metadata.assert_called_once_with(
        response,
        model="azure/gpt-4.1",
        display_model="gpt-4.1",
    )


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
