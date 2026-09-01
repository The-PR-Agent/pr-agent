"""Lifecycle tests for pr_agent.telemetry.shutdown.

These harden the teardown contract: ``shutdown_telemetry()`` must flush any
spans still queued in the batch processor, cascade shutdown through
provider -> span processor -> exporter (freeing outbound exporter
connections), release the provider object graph so memory is reclaimable,
never raise, and be registered with atexit exactly once. ``flush_telemetry()``
is the per-request export used by serverless deployments.

pr-agent hands every provider it creates to the ``registry.provider_registry``
singleton and must never touch OpenTelemetry's process-global provider, which
may belong to a host application embedding pr-agent.
"""

import gc
import weakref

import pytest
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from pr_agent.telemetry import shutdown as shutdown_module
from pr_agent.telemetry.registry import provider_registry
from pr_agent.telemetry.shutdown import (
    flush_telemetry,
    register_shutdown_handler,
    shutdown_telemetry,
)
from tests.unittest._telemetry_helpers import capture_loguru, clear_telemetry_caches, make_config


@pytest.fixture(autouse=True)
def _reset_telemetry():
    clear_telemetry_caches()
    yield
    clear_telemetry_caches()


class RecordingExporter(InMemorySpanExporter):
    """In-memory exporter that records the shutdown/flush cascade order."""

    def __init__(self, events):
        super().__init__()
        self.events = events

    def export(self, spans):
        self.events.append("export")
        return super().export(spans)

    def shutdown(self):
        self.events.append("exporter_shutdown")
        return super().shutdown()

    def force_flush(self, timeout_millis=30000):
        self.events.append("exporter_flush")
        return super().force_flush(timeout_millis)


def _batched_provider(events):
    """TracerProvider whose batch processor never exports on its own."""
    exporter = RecordingExporter(events)
    provider = TracerProvider(shutdown_on_exit=False)
    # Huge schedule delay so nothing exports until a flush/shutdown forces it.
    provider.add_span_processor(BatchSpanProcessor(exporter, schedule_delay_millis=600_000))
    return provider, exporter


def test_shutdown_flushes_pending_spans_then_shuts_down_exporter():
    """Spans still queued in the batch processor must be exported (not lost)
    during shutdown, and the exporter must be shut down afterwards."""
    events = []
    provider, exporter = _batched_provider(events)

    with provider.get_tracer("test").start_as_current_span("pending-span"):
        pass
    assert exporter.get_finished_spans() == (), "span should still be queued, not exported"

    provider_registry.register(provider)
    shutdown_telemetry()

    exported_names = [s.name for s in exporter.get_finished_spans()]
    assert exported_names == ["pending-span"], "queued span must be flushed during shutdown"
    assert "exporter_shutdown" in events, "shutdown must cascade down to the exporter"
    assert events.index("export") < events.index("exporter_shutdown"), \
        "pending spans must be exported before the exporter is torn down"


def test_shutdown_frees_provider_object_graph_memory():
    """After shutdown, dropping our references must actually deallocate the
    provider, processor, and exporter — the registry must not keep shut-down
    providers alive."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)

    with provider.get_tracer("test").start_as_current_span("span-before-shutdown"):
        pass

    provider_ref = weakref.ref(provider)
    processor_ref = weakref.ref(processor)
    exporter_ref = weakref.ref(exporter)

    provider_registry.register(provider)
    shutdown_telemetry()
    assert len(provider_registry) == 0, "shutdown must drop its provider references"

    del provider, processor, exporter
    gc.collect()

    assert provider_ref() is None, "TracerProvider must be deallocated after shutdown"
    assert processor_ref() is None, "span processor must be deallocated after shutdown"
    assert exporter_ref() is None, "exporter (and its buffered spans) must be deallocated"


def test_shutdown_and_flush_are_noops_with_empty_registry():
    """With telemetry disabled nothing was registered, so both lifecycle calls
    must be silent no-ops."""
    assert len(provider_registry) == 0
    shutdown_telemetry()  # must not raise
    flush_telemetry()  # must not raise


def test_shutdown_never_touches_process_global_provider(monkeypatch):
    """The process-global provider may belong to a host application embedding
    pr-agent — the lifecycle calls must not even look at it."""
    def _forbidden():
        raise AssertionError("telemetry lifecycle must not access the global TracerProvider")

    monkeypatch.setattr(trace, "get_tracer_provider", _forbidden)

    provider = TracerProvider(shutdown_on_exit=False)
    provider_registry.register(provider)

    flush_telemetry()  # must not raise (would raise AssertionError if it peeked)
    shutdown_telemetry()


def test_shutdown_swallows_exception_and_still_shuts_down_other_provider():
    """A failing provider shutdown must not propagate — and must not prevent
    later-registered providers from being shut down."""

    class ExplodingProvider:
        def shutdown(self):
            raise RuntimeError("exporter connection reset")

    meter_provider = MeterProvider()
    provider_registry.register(ExplodingProvider())
    provider_registry.register(meter_provider)

    with capture_loguru(level="WARNING") as captured:
        shutdown_telemetry()  # must not raise

    combined = "\n".join(captured)
    assert "Error shutting down telemetry" in combined
    assert "exporter connection reset" in combined
    assert meter_provider._shutdown is True, \
        "meter must still be shut down when the tracer shutdown fails"


def test_flush_exports_queued_spans_without_shutting_down():
    """flush_telemetry is the per-request export used by serverless deployments:
    it must push queued spans out but leave the provider fully usable."""
    events = []
    provider, exporter = _batched_provider(events)
    provider_registry.register(provider)

    with provider.get_tracer("test").start_as_current_span("first-request"):
        pass
    flush_telemetry()

    assert [s.name for s in exporter.get_finished_spans()] == ["first-request"]
    assert "exporter_shutdown" not in events, "flush must not tear anything down"
    assert len(provider_registry) == 1, "flush must keep the provider registered"

    # The provider must still work for the next (warm) invocation.
    with provider.get_tracer("test").start_as_current_span("second-request"):
        pass
    flush_telemetry()
    assert [s.name for s in exporter.get_finished_spans()] == ["first-request", "second-request"]


def test_flush_swallows_errors_and_flushes_remaining_provider():
    class ExplodingProvider:
        def force_flush(self, timeout_millis=3000):
            raise RuntimeError("collector unreachable")

    class RecordingProvider:
        def __init__(self):
            self.flushed = False

        def force_flush(self, timeout_millis=3000):
            self.flushed = True
            return True

    meter_provider = RecordingProvider()
    provider_registry.register(ExplodingProvider())
    provider_registry.register(meter_provider)

    with capture_loguru(level="WARNING") as captured:
        flush_telemetry()  # must not raise

    assert "Error flushing telemetry" in "\n".join(captured)
    assert meter_provider.flushed is True, "meter must still flush when the tracer flush fails"


class _DeadlineRecordingProvider:
    """Records the force_flush deadline it was handed."""

    def __init__(self):
        self.timeout_millis = None

    def force_flush(self, timeout_millis=3000):
        self.timeout_millis = timeout_millis
        return True


def test_flush_deadline_follows_configured_otlp_timeout(monkeypatch):
    """The request-boundary flush must honor OTEL.OTLP_TIMEOUT — raising the
    export deadline must extend the flush wait too, not stop it at 3 s."""
    monkeypatch.setattr(shutdown_module, "get_otel_config", lambda: make_config(otlp_timeout=10))
    provider = _DeadlineRecordingProvider()
    provider_registry.register(provider)

    flush_telemetry()
    assert provider.timeout_millis == 10_000, "flush deadline must be OTEL.OTLP_TIMEOUT in ms"

    flush_telemetry(timeout_millis=500)
    assert provider.timeout_millis == 500, "an explicit deadline must override the configuration"


def test_flush_falls_back_to_default_deadline_when_config_raises(monkeypatch):
    """get_otel_config raises on an invalid OTEL.EXPORTER_TYPE; the flush runs
    in handle_request's finally, so it must still export with the default
    deadline instead of propagating."""
    def _invalid_config():
        raise ValueError("Invalid OTEL.EXPORTER_TYPE 'bogus'")

    monkeypatch.setattr(shutdown_module, "get_otel_config", _invalid_config)
    provider = _DeadlineRecordingProvider()
    provider_registry.register(provider)

    flush_telemetry()  # must not raise

    assert provider.timeout_millis == 3000, "config errors must fall back to the 3 s default"


def test_register_shutdown_handler_registers_atexit_exactly_once(monkeypatch):
    """register_shutdown_handler is lru_cache'd: repeated calls must produce a
    single atexit registration, not one per call."""
    registered = []

    class FakeAtexit:
        @staticmethod
        def register(func):
            registered.append(func)
            return func

    monkeypatch.setattr(shutdown_module, "atexit", FakeAtexit)
    register_shutdown_handler.cache_clear()

    register_shutdown_handler()
    register_shutdown_handler()
    register_shutdown_handler()

    assert registered == [shutdown_telemetry], \
        "atexit must receive shutdown_telemetry exactly once despite repeated calls"
