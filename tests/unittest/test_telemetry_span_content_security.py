"""Span-content security tests for the LiteLLM handler's telemetry helpers.

Caching the tracer itself (lru_cache) is fine and asserted here — but message
content must NEVER be recorded in telemetry. Spans are cached/batched in
memory by the SDK before export, so any prompt or response text placed on a
span would sit in process memory (and ship to the exporter) where it could be
scanned. These tests pin the span attribute keys to an exact allowlist and
prove a sentinel message string never survives into exported span data.
"""

from types import SimpleNamespace

import pytest

from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.telemetry import tracer as tracer_module
from pr_agent.telemetry.tracer import get_tracer
from tests.unittest._telemetry_helpers import build_in_memory_tracer, clear_telemetry_caches, make_config

SENTINEL = "SENTINEL-MESSAGE-CONTENT-MUST-NOT-BE-TRACED"


@pytest.fixture(autouse=True)
def _reset_telemetry():
    clear_telemetry_caches()
    yield
    clear_telemetry_caches()


@pytest.fixture
def handler():
    # The _set_*_span_attributes helpers use no instance state, so skip the
    # heavy provider-credential __init__ entirely.
    return LiteLLMAIHandler.__new__(LiteLLMAIHandler)


def _finished_span(exporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    return spans[0]


def _assert_span_clean(span, expected_keys):
    """Attribute keys must match the allowlist exactly, and the sentinel must
    not appear anywhere in the exported span (attributes, name, events, status)."""
    assert set(span.attributes.keys()) == expected_keys
    serialized = "".join([
        repr(dict(span.attributes)),
        span.name,
        repr(span.events),
        repr(span.status.status_code),
        repr(span.status.description),
    ])
    assert SENTINEL not in serialized, "message content leaked into exported span data"


def test_get_tracer_lru_cache_identity(monkeypatch):
    """Caching the tracer object is expected behavior — the security boundary
    is span *content*, not tracer identity."""
    monkeypatch.setattr(tracer_module, "get_otel_config", lambda: make_config(is_enabled=False))
    get_tracer.cache_clear()

    assert get_tracer() is get_tracer()


def test_request_attrs_exact_allowlist_no_message_content(handler):
    tracer, exporter = build_in_memory_tracer()
    kwargs = {
        "model": "gpt-test",
        "messages": [
            {"role": "system", "content": SENTINEL},
            {"role": "user", "content": SENTINEL},
        ],
        "temperature": 0.2,
        "max_tokens": 1000,
        "deployment_id": "deploy-1",
    }

    with tracer.start_as_current_span("request") as span:
        handler._set_request_span_attributes(span, kwargs["model"], kwargs)

    _assert_span_clean(_finished_span(exporter), {
        "litellm.request.model",
        "litellm.system",
        "litellm.request.temperature",
        "litellm.request.max_tokens",
        "litellm.request.deployment_id",
    })


def test_request_attrs_minimal_kwargs(handler):
    """With only model+messages present, nothing derived from messages may be
    recorded — the key set shrinks to the two static attributes."""
    tracer, exporter = build_in_memory_tracer()
    kwargs = {
        "model": "gpt-test",
        "messages": [{"role": "user", "content": SENTINEL}],
    }

    with tracer.start_as_current_span("request") as span:
        handler._set_request_span_attributes(span, kwargs["model"], kwargs)

    _assert_span_clean(_finished_span(exporter), {
        "litellm.request.model",
        "litellm.system",
    })


def test_streaming_response_attrs_allowlist(handler):
    tracer, exporter = build_in_memory_tracer()
    # Response carries the sentinel in its content-bearing fields; the helper
    # must only read .id and the finish_reason passed alongside.
    response = SimpleNamespace(
        id="resp-stream-1",
        choices=[SimpleNamespace(delta=SimpleNamespace(content=SENTINEL))],
        text=SENTINEL,
    )

    with tracer.start_as_current_span("streaming-response") as span:
        handler._set_streaming_response_span_attributes(span, response, "stop")

    span = _finished_span(exporter)
    _assert_span_clean(span, {
        "litellm.response.id",
        "litellm.response.finish_reason",
        "litellm.response.streaming",
    })
    assert span.attributes["litellm.response.streaming"] is True
    assert span.attributes["litellm.response.finish_reason"] == "stop"


class _FakeUsage:
    def __init__(self, with_reasoning=True):
        self.prompt_tokens = 100
        self.completion_tokens = 40
        self.total_tokens = 140
        if with_reasoning:
            self.completion_tokens_details = SimpleNamespace(reasoning_tokens=25)


class _FakeResponse:
    """Shaped like a LiteLLM ModelResponse: attribute access for id/usage and
    dict-style subscription for choices."""

    def __init__(self, usage):
        self.id = "resp-1"
        if usage is not None:
            self.usage = usage
        self._data = {
            "choices": [
                {"finish_reason": "stop", "message": {"content": SENTINEL}},
            ]
        }

    def __getitem__(self, key):
        return self._data[key]


def test_response_attrs_allowlist_with_usage_and_reasoning(handler):
    tracer, exporter = build_in_memory_tracer()

    with tracer.start_as_current_span("response") as span:
        handler._set_response_span_attributes(span, _FakeResponse(_FakeUsage()))

    span = _finished_span(exporter)
    _assert_span_clean(span, {
        "litellm.response.id",
        "litellm.response.streaming",
        "litellm.usage.prompt_tokens",
        "litellm.usage.completion_tokens",
        "litellm.usage.total_tokens",
        "litellm.usage.reasoning_tokens",
        "litellm.response.finish_reason",
    })
    assert span.attributes["litellm.response.streaming"] is False
    assert span.attributes["litellm.usage.total_tokens"] == 140
    assert span.attributes["litellm.usage.reasoning_tokens"] == 25


def test_response_attrs_without_usage_omits_usage_keys(handler):
    tracer, exporter = build_in_memory_tracer()

    with tracer.start_as_current_span("response") as span:
        handler._set_response_span_attributes(span, _FakeResponse(usage=None))

    _assert_span_clean(_finished_span(exporter), {
        "litellm.response.id",
        "litellm.response.streaming",
        "litellm.response.finish_reason",
    })


def test_exporter_retains_no_reference_to_request_messages(handler):
    """The exporter boundary is where spans leave the process (or, for the
    in-memory exporter, where the SDK's cached copy lives). Nothing reachable
    from an exported span may contain the message content."""
    tracer, exporter = build_in_memory_tracer()
    kwargs = {
        "model": "gpt-test",
        "messages": [{"role": "user", "content": SENTINEL}],
        "temperature": 0.2,
        "max_tokens": 512,
    }

    with tracer.start_as_current_span("request") as span:
        handler._set_request_span_attributes(span, kwargs["model"], kwargs)
    with tracer.start_as_current_span("response") as span:
        handler._set_response_span_attributes(span, _FakeResponse(_FakeUsage()))

    for exported in exporter.get_finished_spans():
        assert SENTINEL not in exported.to_json(), \
            "exported span serialization must not contain message content"
