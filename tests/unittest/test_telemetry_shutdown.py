"""Shutdown-path tests for pr_agent.telemetry.shutdown.

These harden the teardown contract: ``shutdown_telemetry()`` must flush any
spans still queued in the batch processor, cascade shutdown through
provider -> span processor -> exporter (freeing outbound exporter
connections), release the provider object graph so memory is reclaimable,
never raise, and be registered with atexit exactly once.
"""

import gc
import weakref

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from pr_agent.telemetry import shutdown as shutdown_module
from pr_agent.telemetry.shutdown import register_shutdown_handler, shutdown_telemetry
from tests.unittest._telemetry_helpers import capture_loguru, clear_telemetry_caches


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


def test_shutdown_flushes_pending_spans_then_shuts_down_exporter(monkeypatch):
    """Spans still queued in the batch processor must be exported (not lost)
    during shutdown, and the exporter must be shut down afterwards."""
    events = []
    exporter = RecordingExporter(events)
    provider = TracerProvider(shutdown_on_exit=False)
    # Huge schedule delay so nothing exports until shutdown forces the flush.
    provider.add_span_processor(BatchSpanProcessor(exporter, schedule_delay_millis=600_000))

    with provider.get_tracer("test").start_as_current_span("pending-span"):
        pass
    assert exporter.get_finished_spans() == (), "span should still be queued, not exported"

    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    shutdown_telemetry()

    exported_names = [s.name for s in exporter.get_finished_spans()]
    assert exported_names == ["pending-span"], "queued span must be flushed during shutdown"
    assert "exporter_shutdown" in events, "shutdown must cascade down to the exporter"
    assert events.index("export") < events.index("exporter_shutdown"), \
        "pending spans must be exported before the exporter is torn down"


def test_shutdown_frees_provider_object_graph_memory(monkeypatch):
    """After shutdown, dropping our references must actually deallocate the
    provider, processor, and exporter (no hidden strong refs keep them alive)."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)

    with provider.get_tracer("test").start_as_current_span("span-before-shutdown"):
        pass

    provider_ref = weakref.ref(provider)
    processor_ref = weakref.ref(processor)
    exporter_ref = weakref.ref(exporter)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(trace, "get_tracer_provider", lambda: provider)
        shutdown_telemetry()

    del provider, processor, exporter
    gc.collect()

    assert provider_ref() is None, "TracerProvider must be deallocated after shutdown"
    assert processor_ref() is None, "span processor must be deallocated after shutdown"
    assert exporter_ref() is None, "exporter (and its buffered spans) must be deallocated"


def test_shutdown_closes_exporter_connection(monkeypatch):
    """The shutdown cascade must reach exporter.shutdown() — the hook where
    real exporters (e.g. OTLP/gRPC) close their outbound channels."""

    class ConnectionExporter(InMemorySpanExporter):
        def __init__(self):
            super().__init__()
            self.connection_open = True

        def shutdown(self):
            self.connection_open = False
            return super().shutdown()

    exporter = ConnectionExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    shutdown_telemetry()

    assert exporter.connection_open is False, "outbound exporter connection must be closed"


def test_shutdown_noop_when_provider_has_no_shutdown(monkeypatch):
    """The default (no-op) provider has no shutdown(); the hasattr guard must
    make this a silent no-op instead of an AttributeError."""
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: object())
    shutdown_telemetry()  # must not raise


def test_shutdown_swallows_exception_and_warns(monkeypatch):
    """A failing provider shutdown must never propagate out of atexit — it is
    logged as a warning instead."""

    class ExplodingProvider:
        def shutdown(self):
            raise RuntimeError("exporter connection reset")

    monkeypatch.setattr(trace, "get_tracer_provider", lambda: ExplodingProvider())

    with capture_loguru(level="WARNING") as captured:
        shutdown_telemetry()  # must not raise

    combined = "\n".join(captured)
    assert "Error shutting down telemetry" in combined
    assert "exporter connection reset" in combined


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
